/**
 * 财报筛选独立页（应收账款表）。
 * 业务逻辑以本文件为唯一维护位置（已从 rt_monitor.js 剥离）；勿在主监控脚本中重复修改同功能。
 * 若曾保存「含财报段」的全量 rt_monitor.js 备份，可用 tools/build_rt_monitor_fina_slice.py 对照再生成。
 */
/** 前端错误日志：统一前缀 [rt_monitor]，便于过滤；仅在有 console 时输出，避免无 console 环境报错 */
function logError(msg, detail) {
    if (typeof window !== 'undefined' && window.console && window.console.error) {
        if (detail !== undefined) window.console.error('[rt_monitor] ' + msg, detail);
        else window.console.error('[rt_monitor] ' + msg);
    }
}
function rtMonitorApiUrl(path) {
    if (path == null || path === '') return path;
    var p = String(path);
    if (p.charAt(0) !== '/') p = '/' + p;
    var pre = typeof window !== 'undefined' ? window.__RT_MONITOR_API_PREFIX__ : '';
    pre = pre != null && pre !== '' ? String(pre).replace(/\/+$/, '') : '';
    return pre ? pre + p : p;
}
/** 财报表首屏：与 #fina-boot-hint 成对，避免 fetch 挂起或链上异常时永远停留在「正在加载…」 */
function hideFinaBootHint() {
    var el = document.getElementById('fina-boot-hint');
    if (el) el.style.display = 'none';
}
/** 主表 API 无默认超时，慢库会长期 pending；到点 abort 以便走 catch 并收起首屏提示 */
var FINA_METRIC_TABLE_FETCH_TIMEOUT_MS = 240000;
var FINA_ARTURN_SCHEME_STORAGE_KEY = 'rt_monitor_fina_arturn_filter_schemes_v1';
var FINA_ARTURN_VIEW_STATE_STORAGE_KEY = 'rt_monitor_fina_arturn_view_state_v1';
var FINA_ARTURN_SCHEME_APPLY_PREF_KEY = 'rt_monitor_fina_arturn_scheme_apply_pref_v1';
var finaFixedAssetsChartInstance = null;
var finaCashCreateChartInstance = null;
var finaWorkingCapTurnoverChartInstance = null;
var finaCapEffChartInstance = null;
var finaFixedAssetsCurrentRows = [];
var finaFixedAssetsCurrentTitle = '';
var FINA_FIXED_ASSETS_SERIES_LIMIT = 25;
/** 表头合并文案中的图表主题（与模板默认一致） */
var FINA_FIXED_ASSETS_HEADER_CHART_TITLE = '速览';
var FINA_FIXED_ASSETS_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_fina_fixed_assets_modal_layout_v1';
var FINA_FIXED_ASSETS_GROWTH_CONFIG_STORAGE_KEY = 'rt_monitor_fina_fixed_assets_growth_metrics_v1';
var FINA_FIXED_ASSETS_GROWTH_METRIC_OPTIONS = [
    { label: '营业收入同比(%)', key: 'q_gr_yoy' },
    { label: '营业收入环比(%)', key: 'q_gr_qoq' },
    { label: '营业利润同比(%)', key: 'q_op_yoy' },
    { label: '营业利润环比(%)', key: 'q_op_qoq' },
    { label: '归母净利同比(%)', key: 'q_netprofit_yoy' },
    { label: '归母净利环比(%)', key: 'q_netprofit_qoq' },
];
var finaFixedAssetsModalLayoutOpenTs = 0;
var finaFixedAssetsModalSaveTimer = null;
var FINA_CASH_CREATE_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_fina_cash_create_modal_layout_v1';
var finaCashCreateModalLayoutOpenTs = 0;
var finaCashCreateModalSaveTimer = null;
var FINA_WORKING_CAP_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_fina_working_cap_modal_layout_v1';
var finaWorkingCapModalLayoutOpenTs = 0;
var finaWorkingCapModalSaveTimer = null;
var FINA_CAP_EFF_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_fina_cap_eff_modal_layout_v1';
var finaCapEffModalLayoutOpenTs = 0;
var finaCapEffModalSaveTimer = null;
var FINA_SCHEME_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_fina_arturn_scheme_modal_layout_v1';
var finaSchemeModalLayoutOpenTs = 0;
var finaSchemeModalSaveTimer = null;
/** 弹窗几何快照（px）：用于关窗时避免同步 getBoundingClientRect 触发整页（含大表 sticky）回流卡顿 */
var finaSchemeModalLayoutMemory = null;
var _finaModalOpenCount = 0;
function _modalOpened() {
    _finaModalOpenCount += 1;
}
function _modalClosed() {
    _finaModalOpenCount = Math.max(0, _finaModalOpenCount - 1);
}
/** 与主监控 BIAS 弹窗 showBiasSeriesMsg 同款：仅在有文案时展开 absolute 遮罩，避免盖住 canvas */
function showFinaFixedAssetsMsg(text) {
    var el = document.getElementById('fina-fixed-assets-modal-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = text ? 'flex' : 'none';
}
function showFinaCashCreateMsg(text) {
    var el = document.getElementById('fina-cash-create-modal-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = text ? 'flex' : 'none';
}
function showFinaWorkingCapMsg(text) {
    var el = document.getElementById('fina-working-cap-modal-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = text ? 'flex' : 'none';
}
function showFinaCapEffMsg(text) {
    var el = document.getElementById('fina-cap-eff-modal-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = text ? 'flex' : 'none';
}
var finaArturnState = {
    selectedEndDate: '',
    periods: [],
    contractPeriods: [],
    rows: [],
    endDates: [],
    filters: {
        tsCode: '', secName: '', swName: '',
        total_mv_yi: '', free_mv_yi: '',
        a0: '', listDate: '',
        money_cap_ratio_pct: '',
        fqm_debt_to_assets: '',
        fqm_interestdebt_ratio: '',
        ttm_revenue_yi: '',
        pe_ttm: '',
        ar_pct: '',
        roe0: '', roe1: '', roe2: '', roe3: '',
        p0: '', p1: '', p2: '', p3: '',
        g0: '', g1: '', g2: '', g3: '',
        c0: '', c1: '', c2: '', c3: '',
        o0: '', o1: '', o2: '', o3: '',
        s_high: '',
        cf_high: '',
        cf0: '', cf1: '', cf2: '', cf3: '', cf4: '', cf5: '', cf6: '', cf7: '', cf8: '',
        oi_high: '',
        oi0: '', oi1: '', oi2: '', oi3: '', oi4: '', oi5: '', oi6: '', oi7: '', oi8: '',
        dtp_high: '',
        dtp0: '', dtp1: '', dtp2: '', dtp3: '', dtp4: '', dtp5: '', dtp6: '', dtp7: '', dtp8: '',
        fst_high: '',
        fst0: '', fst1: '', fst2: '', fst3: '', fst4: '', fst5: '', fst6: '', fst7: '', fst8: '',
        ocs_high: '',
        ocs_wqcv: '',
        ocs0: '', ocs1: '', ocs2: '', ocs3: '', ocs4: '', ocs5: '', ocs6: '', ocs7: '', ocs8: '',
        oco0: '', oco1: '', oco2: '', oco3: '', oco4: '', oco5: '', oco6: '', oco7: '', oco8: '',
        r0: '', r1: '', r2: '', r3: '', r4: '', r5: '', r6: '', r7: '', r8: '',
        m_high: '', m_qoq: '',
        m0: '', m1: '', m2: '', m3: '', m4: '', m5: '', m6: '', m7: '', m8: '',
        n0: '', n1: '', n2: '', n3: '', n4: '', n5: '', n6: '', n7: '', n8: '',
        t0: '', t1: '', t2: '', t3: '', t4: '', t5: '', t6: '', t7: '', t8: '',
        invchg: '',
        inv_revenue_pct: '',
        inv_high: '',
        inv0: '', inv1: '', inv2: '', inv3: '', inv4: '', inv5: '', inv6: '', inv7: '', inv8: '',
        kchg: '',
        k_revenue_pct: '',
        k_high: '',
        k0: '', k1: '', k2: '', k3: '', k4: '', k5: '', k6: '', k7: '', k8: '',
        fchg: '',
        f0: '', f1: '', f2: '', f3: '', f4: '', f5: '', f6: '', f7: '',         f8: '',
        u0: '', u1: '', u2: '', u3: '', u4: '', u5: '', u6: '', u7: '', u8: '',
        v0: '', v1: '', v2: '', v3: '', v4: '', v5: '', v6: '', v7: '', v8: ''
    },
    activeFilterKey: '',
    /** 排序：sortKey 为空表示不排序；sortDir 为 asc|desc（仅当 sortKey 非空时有效） */
    sortKey: '',
    sortDir: 'asc',
    /** 配置弹窗：列表是否排除北交所 .BJ、ST 股（与列筛选独立，存 viewState） */
    excludeBj: false,
    excludeSt: false,
    /** 与当前 tbody 顺序一致（筛选+排序后），供固定资产弹窗「上一个/下一个」 */
    fixedAssetsNavList: []
};
/** ROE（扣非）+ 周转天+毛利率+两项经营现金流比率：DOM id 与接口路径 */
var FINA_METRIC_PANEL = {
    arturn: {
        apiPath: '/api/fina_arturn_days_table',
        schemeStorageKey: FINA_ARTURN_SCHEME_STORAGE_KEY,
        viewStateStorageKey: FINA_ARTURN_VIEW_STATE_STORAGE_KEY,
        logLabel: 'fina_arturn_days_table',
        ids: {
            section: 'fina-arturn-section',
            thead: 'fina-arturn-thead',
            tbody: 'fina-arturn-tbody',
            /** 分栏外包裹：委托固定资产弹窗点击（覆盖冻结+可滚动 tbody，单一监听器） */
            splitWrap: 'fina-arturn-tbody-scroll',
            msg: 'fina-arturn-msg',
            headUpdated: 'fina-arturn-head-updated',
            schemeOpen: 'fina-arturn-scheme-open',
            filterPopover: 'fina-arturn-filter-popover',
            filterTitle: 'fina-filter-title',
            filterInput: 'fina-filter-popover-input',
            filterApply: 'fina-filter-popover-apply',
            filterClear: 'fina-filter-popover-clear',
            filterClose: 'fina-filter-popover-close',
            schemeOverlay: 'fina-arturn-scheme-modal-overlay',
            schemeClose: 'fina-arturn-scheme-modal-close',
            schemeModalMsg: 'fina-arturn-scheme-modal-msg',
            schemeEndDate: 'fina-arturn-end-date-select',
            schemeName: 'fina-arturn-scheme-name',
            schemeSave: 'fina-arturn-save-scheme-btn',
            schemeSelect: 'fina-arturn-scheme-select',
            schemeApplyEndDateMode: 'fina-arturn-apply-end-date-mode',
            schemeApplyEndDateManualWrap: 'fina-arturn-apply-end-date-manual-wrap',
            schemeApplyEndDateSelect: 'fina-arturn-apply-end-date-select',
            schemeApplyRememberPref: 'fina-arturn-apply-end-date-remember',
            schemeApply: 'fina-arturn-apply-scheme-btn',
            schemeDelete: 'fina-arturn-delete-scheme-btn',
            schemeResetFilters: 'fina-arturn-reset-filters-btn',
            schemePreviewWrap: 'fina-arturn-scheme-preview-wrap',
            schemePreviewBody: 'fina-arturn-scheme-preview-body',
            excludeBj: 'fina-arturn-exclude-bj',
            excludeSt: 'fina-arturn-exclude-st'
        }
    }
};
function _finaMetricState() {
    return finaArturnState;
}

/* ==================== 筛选弹窗：列类型 + per-key 历史记录（v20260423-47） ====================
   需求：
    1. 打开筛选弹窗时按「当前值 > 最近一次历史 > 空」顺序预填；用户按「确定」即应用
    2. 历史按列独立存储（LRU 去重 max 12 条），「重置所有筛选」不清 history
    3. 历史值按列类型过滤：
         number   → 仅保留 >= < > <= = != 数值 / a-b 区间 / 纯数
         date     → 仅保留 yyyymmdd / yyyy-mm-dd / yyyy/mm/dd / 纯数字日期片段
         binary01 → 仅保留 0/1（含 =0/=1）
         text     → 任意非空字符串
*/
var FINA_FILTER_TYPE = {
    tsCode: 'text', secName: 'text', swName: 'text',
    a0: 'date', listDate: 'date',
    s_high: 'binary01', cf_high: 'binary01', oi_high: 'binary01',
    dtp_high: 'binary01', fst_high: 'binary01', ocs_high: 'binary01',
    inv_high: 'binary01', k_high: 'binary01'
};
function finaMetricFilterKeyType(key) {
    return FINA_FILTER_TYPE[key] || 'number';
}
var FINA_FILTER_HISTORY_KEY = 'fina-arturn-filter-history';
var FINA_FILTER_HISTORY_MAX = 12;
function loadFinaFilterHistory() {
    try {
        var raw = localStorage.getItem(FINA_FILTER_HISTORY_KEY);
        if (!raw) return {};
        var obj = JSON.parse(raw);
        return (obj && typeof obj === 'object') ? obj : {};
    } catch (e) { return {}; }
}
function saveFinaFilterHistory(obj) {
    try { localStorage.setItem(FINA_FILTER_HISTORY_KEY, JSON.stringify(obj || {})); }
    catch (e) { /* quota / 无痕模式，忽略 */ }
}
function pushFinaFilterHistory(filterKey, value) {
    if (!filterKey) return;
    var v = String(value == null ? '' : value).trim();
    if (!v) return;
    var all = loadFinaFilterHistory();
    var list = Array.isArray(all[filterKey]) ? all[filterKey] : [];
    /* 去重（大小写不敏感 + 去空白）：先剔除已存在的同值项 */
    var vLow = v.toLowerCase();
    list = list.filter(function (x) { return String(x || '').trim().toLowerCase() !== vLow; });
    list.unshift(v);
    if (list.length > FINA_FILTER_HISTORY_MAX) list.length = FINA_FILTER_HISTORY_MAX;
    all[filterKey] = list;
    saveFinaFilterHistory(all);
}
/* 数值表达式：支持 >= <= > < = != + 数字（可带小数/负号/百分号），或区间 a-b（b 可空），或纯数 */
var _FINA_NUM_RE = /^\s*(?:(?:>=|<=|!=|>|<|=)\s*-?\d+(?:\.\d+)?%?|-?\d+(?:\.\d+)?%?(?:\s*-\s*-?\d+(?:\.\d+)?%?)?)\s*$/;
/* 日期：yyyymmdd(6-8 位数字)、yyyy-mm-dd / yyyy/mm/dd（年/月/日位数放宽），或带比较符 */
var _FINA_DATE_RE = /^\s*(?:(?:>=|<=|!=|>|<|=)\s*)?(?:\d{4,8}|\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?|\d{2}[-/]\d{1,2})\s*$/;
var _FINA_BIN_RE = /^\s*(?:=?\s*[01])\s*$/;
function isFinaFilterValueValid(value, type) {
    var v = String(value == null ? '' : value).trim();
    if (!v) return false;
    if (type === 'text') return true;
    if (type === 'binary01') return _FINA_BIN_RE.test(v);
    if (type === 'date') return _FINA_DATE_RE.test(v);
    /* number：默认 */
    return _FINA_NUM_RE.test(v);
}
/* 取过滤后的历史值列表（按列类型） */
function getFinaFilterHistoryFor(filterKey) {
    var list = loadFinaFilterHistory()[filterKey];
    if (!Array.isArray(list)) return [];
    var type = finaMetricFilterKeyType(filterKey);
    return list.filter(function (v) { return isFinaFilterValueValid(v, type); });
}

/**
 * 标记 rows 数据已变化（fetch 新数据时调用）：
 *  - _rowsVersion 自增 → 触发下次 render 走全量重建而非快路径
 *  - 清空 tr Map（旧引用已失效）
 *  - 重置 DOM 构建版本
 *  - 给每个 row 打 _origIdx 标记（供 filter/sort 快路径按原始索引定位 tr）
 */
function markFinaMetricRowsChanged(st) {
    st._rowsVersion = (st._rowsVersion || 0) + 1;
    st._domBuiltRowsVersion = -1;
    st._frozenTrMap = null;
    st._scrollTrMap = null;
    if (Array.isArray(st.rows)) {
        for (var i = 0; i < st.rows.length; i++) {
            if (st.rows[i] && typeof st.rows[i] === 'object') {
                st.rows[i]._origIdx = i;
            }
        }
    }
}

function loadFinaMetricViewState(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg || !cfg.viewStateStorageKey) return null;
    try {
        var raw = localStorage.getItem(cfg.viewStateStorageKey);
        if (!raw) return null;
        var data = JSON.parse(raw);
        if (!data || typeof data !== 'object') return null;
        return {
            selectedEndDate: String(data.selectedEndDate || ''),
            filters: normalizeFinaMetricFilterShape(data.filters),
            sortKey: String(data.sortKey || ''),
            sortDir: data.sortDir === 'desc' ? 'desc' : 'asc',
            excludeBj: data.excludeBj === true,
            excludeSt: data.excludeSt === true
        };
    } catch (e) {
        return null;
    }
}

function saveFinaMetricViewState(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg || !cfg.viewStateStorageKey) return;
    try {
        var st = _finaMetricState();
        var payload = {
            selectedEndDate: String(st.selectedEndDate || ''),
            filters: getFinaMetricFilters(panelKey),
            sortKey: String(st.sortKey || ''),
            sortDir: st.sortDir === 'desc' ? 'desc' : 'asc',
            excludeBj: st.excludeBj === true,
            excludeSt: st.excludeSt === true
        };
        localStorage.setItem(cfg.viewStateStorageKey, JSON.stringify(payload));
    } catch (e) {
        /* ignore localStorage failures */
    }
}

/** 证券简称是否视为 ST、*ST、S*ST、退市整理（配置「去除 ST」） */
function finaArturnIsStStockName(secName) {
    var s = String(secName || '').trim();
    if (!s) return false;
    if (/^退/.test(s)) return true;
    if (/^S[\*＊]ST/i.test(s)) return true;
    if (/^[\*＊]ST/i.test(s)) return true;
    if (/^ST/i.test(s)) return true;
    return false;
}

/** 申万二级行业：按英文单词 or（不区分大小写）拆成多段，段内仍为包含匹配，段之间为 OR */
function finaArturnSplitSwNameOrKeywords(exprRaw) {
    var s = String(exprRaw || '').trim();
    if (!s) return [];
    return s
        .split(/\s*\bor\b\s*/i)
        .map(function (p) {
            return p.trim();
        })
        .filter(function (p) {
            return p.length > 0;
        });
}

function syncFinaMetricStockScopeCheckboxes(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var st = _finaMetricState();
    var elBj = document.getElementById(cfg.ids.excludeBj);
    var elSt = document.getElementById(cfg.ids.excludeSt);
    if (elBj) elBj.checked = st.excludeBj === true;
    if (elSt) elSt.checked = st.excludeSt === true;
}

function formatFinaArturnValue(v, decimals) {
    if (v === null || v === undefined || v === '') return '--';
    var n = parseFloat(v);
    if (isNaN(n)) return '--';
    var d = decimals === undefined || decimals === null ? 2 : decimals;
    return n.toFixed(d);
}

function formatFinaQuarterLabel(endDate) {
    var s = String(endDate || '');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return s || '--';
    var y = s.slice(0, 4);
    var m = s.slice(5, 7);
    var q = 'Q4';
    if (m === '03') q = 'Q1';
    else if (m === '06') q = 'Q2';
    else if (m === '09') q = 'Q3';
    return y + '-' + q;
}

/** 与主监控 formatAxisInt 一致：Y 轴整数、无千分位 */
var FINA_CHART_PAGE_FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif';
/** 固定资产弹窗图：保证高分屏/缩放下的绘制像素密度，降低「发虚」观感 */
function getFinaFixedAssetsChartDpr() {
    var dpr = window.devicePixelRatio || 1;
    if (!isFinite(dpr) || dpr <= 0) dpr = 1;
    return Math.min(3, Math.max(2, dpr));
}
/** 按点数自适应字号：点少可读性优先，点多避免重叠 */
function getFinaFixedAssetsChartTextPreset(pointCount) {
    var n = Number(pointCount) || 0;
    if (n <= 0) {
        return { axisFontSize: 11, labelFontSize: 12, labelStrokeWidth: 3 };
    }
    if (n <= 12) {
        return { axisFontSize: 12, labelFontSize: 13, labelStrokeWidth: 3 };
    }
    if (n <= 20) {
        return { axisFontSize: 11, labelFontSize: 12, labelStrokeWidth: 3 };
    }
    if (n <= 28) {
        return { axisFontSize: 10, labelFontSize: 11, labelStrokeWidth: 2.5 };
    }
    return { axisFontSize: 9, labelFontSize: 10, labelStrokeWidth: 2 };
}
function finaFixedAssetsAxisInt(val) {
    if (val == null || (typeof val !== 'number' && isNaN(Number(val)))) return val;
    return String(Math.round(Number(val)));
}

/** 右轴合同负债等与柱顶标签：保留一位小数 */
function finaFixedAssetsAxisOneDecimal(val) {
    if (val == null || (typeof val !== 'number' && isNaN(Number(val)))) return val;
    return Number(val).toFixed(1);
}

/** 固定资产弹窗两图共用图例：字号/字重/字色一致（此前下图 legend 为 10px、上图 11px） */
function finaFixedAssetsLegendOptions() {
    return {
        display: true,
        position: 'top',
        align: 'center',
        padding: { top: 2, bottom: 6, left: 2, right: 2 },
        labels: {
            boxWidth: 12,
            boxHeight: 10,
            padding: 6,
            font: {
                size: 11,
                family: FINA_CHART_PAGE_FONT,
                weight: '500',
            },
            color: '#1e293b',
            usePointStyle: false,
        },
    };
}

/**
 * 双图配色语义对齐：单季营收=紫（两图柱同色）；固定资产=蓝；对照季青柱；
 * 合同负债=玫红折线（橙留给第二张「经营现金流/营收%」，避免两图「橙线」含义打架）。
 */
var FINA_FIXED_ASSETS_DUAL_PAL = {
    revenueFill: 'rgba(139, 92, 246, 0.76)',
    revenueBorder: 'rgba(124, 58, 237, 1)',
    revenueRefFill: 'rgba(34, 211, 238, 0.82)',
    revenueRefBorder: 'rgba(8, 145, 178, 1)',
    fixFill: 'rgba(59, 130, 246, 0.82)',
    fixBorder: 'rgba(37, 99, 235, 1)',
    cipFill: 'rgba(245, 158, 11, 0.78)',
    cipBorder: 'rgba(217, 119, 6, 1)',
    contractLine: 'rgba(219, 39, 119, 1)',
    contractFillSoft: 'rgba(219, 39, 119, 0.06)',
    contractGlow: 'rgba(219, 39, 119, 0.13)',
    cfBarFill: 'rgba(16, 185, 129, 0.76)',
    cfBarBorder: 'rgba(5, 122, 85, 1)',
    opiBarFill: 'rgba(79, 70, 229, 0.78)',
    opiBarBorder: 'rgba(67, 56, 202, 1)',
    ratioOcfOrLine: 'rgba(234, 88, 12, 1)',
    ratioGmLine: 'rgba(109, 40, 217, 1)',
    ratioRoeLine: 'rgba(13, 148, 136, 1)',
};

function clearFinaFixedAssetsMetricsBar() {
    var el = document.getElementById('fina-fixed-assets-modal-metrics');
    if (el) el.innerHTML = '';
}

function clearFinaFixedAssetsMainbzTable() {
    var el = document.getElementById('fina-fixed-assets-mainbz-wrap');
    if (el) el.innerHTML = '';
}

function renderFinaFixedAssetsMainbzTable(payload) {
    var el = document.getElementById('fina-fixed-assets-mainbz-wrap');
    if (!el) return;
    var p = payload && payload.mainbz_top3 ? payload.mainbz_top3 : null;
    function hasRows(sec) {
        if (!sec) return false;
        var rev = Array.isArray(sec.revenue_top3) ? sec.revenue_top3 : [];
        var cost = Array.isArray(sec.cost_top3) ? sec.cost_top3 : [];
        var profit = Array.isArray(sec.profit_top3) ? sec.profit_top3 : [];
        return rev.length > 0 || cost.length > 0 || profit.length > 0;
    }
    function rowAt(arr, idx) {
        return idx < arr.length ? (arr[idx] || {}) : {};
    }
    function fmtYi(v) {
        if (v === null || v === undefined || v === '') return '--';
        var n = parseFloat(v);
        if (isNaN(n)) return '--';
        return n.toFixed(1);
    }
    function fmtPct(v) {
        if (v === null || v === undefined || v === '') return '--';
        var n = parseFloat(v);
        if (isNaN(n)) return '--';
        return n.toFixed(2);
    }
    function fmtGrossMargin(profitYi, revenueYi) {
        var p = parseFloat(profitYi);
        var r = parseFloat(revenueYi);
        if (isNaN(p) || isNaN(r) || r === 0) return '--';
        return ((p / r) * 100.0).toFixed(2);
    }
    function esc(v) {
        return _finaArturnEscCellText(v == null ? '--' : String(v));
    }
    function fmtEndDate(v) {
        var s = String(v == null ? '' : v).trim();
        if (!s) return '--';
        if (/^\d{8}$/.test(s)) return s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8);
        if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
        return s;
    }
    function asNum(v) {
        var n = parseFloat(v);
        return isNaN(n) ? null : n;
    }
    function calcGrowth(curVal, prevVal) {
        var c = asNum(curVal);
        var p0 = asNum(prevVal);
        if (c === null || p0 === null || p0 === 0) return null;
        return ((c / p0) - 1.0) * 100.0;
    }
    function calcDiff(curVal, prevVal) {
        var c = asNum(curVal);
        var p0 = asNum(prevVal);
        if (c === null || p0 === null) return null;
        return c - p0;
    }
    function fmtDelta(v) {
        var n = asNum(v);
        if (n === null) return '--';
        return n.toFixed(2);
    }
    function deltaClass(v) {
        var n = asNum(v);
        if (n === null) return '';
        if (n > 0) return ' mainbz-delta-pos';
        if (n < 0) return ' mainbz-delta-neg';
        return '';
    }
    /** 分项占合计%；与后端 tot_*_yi 同口径（亿） */
    function pctOfYi(partYi, totYi) {
        var p = asNum(partYi);
        var t = asNum(totYi);
        if (p === null || t === null || t === 0) return '--';
        return ((p / t) * 100.0).toFixed(2);
    }
    function buildSnapshotTable(snapshot, compareSnapshot) {
        if (!snapshot) return '';
        var sections = snapshot.sections ? snapshot.sections : null;
        var secP = sections && sections.P ? sections.P : null;
        var secI = sections && sections.I ? sections.I : null;
        var secD = sections && sections.D ? sections.D : null;
        if (!hasRows(secP) && !hasRows(secI) && !hasRows(secD)) return '';
        var reportEnd = snapshot.end_date || (secP && secP.end_date) || '';
        var rows = [];
        function pushSectionRows(secKey, secLabel, sec) {
            var rev = sec && Array.isArray(sec.revenue_top3) ? sec.revenue_top3 : [];
            var curMetrics = sec && sec.item_metrics ? sec.item_metrics : {};
            var prevSections = compareSnapshot && compareSnapshot.sections ? compareSnapshot.sections : {};
            var prevSec = prevSections && prevSections[secKey] ? prevSections[secKey] : {};
            var prevMetrics = prevSec && prevSec.item_metrics ? prevSec.item_metrics : {};
            var totRevYi = sec && sec.tot_revenue_yi != null ? sec.tot_revenue_yi : null;
            var totCostYi = sec && sec.tot_cost_yi != null ? sec.tot_cost_yi : null;
            var totProfitYi = sec && sec.tot_profit_yi != null ? sec.tot_profit_yi : null;
            /* 同比口径（与行序/排名无关）：
             * 仅在本 secKey（P/I/D）内，用当前行 bz_item 名称在 curMetrics、prevMetrics 中做字典查找；
             * 上年对照值来自「次新年报 + 同区块 + 同名分项」，与上年该分项在收入榜第几名无关。
             * revenue_top3 下标只决定「展示哪三个名称」，不参与跨年配对。缺匹配或缺数则 --。 */
            var i = 0;
            for (; i < 3; i += 1) {
                var rr = rowAt(rev, i);
                var itemName = rr && rr.bz_item ? String(rr.bz_item).trim() : '';
                var m = itemName && curMetrics[itemName] ? curMetrics[itemName] : null;
                /* 金额与占比均按分项名取自 item_metrics；仅当聚合映射缺失时用本行收入 Top3 的 amount_yi（仍为同一 bz_item） */
                var revYi = m && m.revenue_yi != null ? m.revenue_yi : (rr && rr.amount_yi != null ? rr.amount_yi : null);
                var costYi = m && m.cost_yi != null ? m.cost_yi : null;
                var profitYi = m && m.profit_yi != null ? m.profit_yi : null;
                var revPct = pctOfYi(revYi, totRevYi);
                var costPct = pctOfYi(costYi, totCostYi);
                var profitPct = pctOfYi(profitYi, totProfitYi);
                var gmStr = '--';
                if (m) {
                    if (m.gross_margin != null) {
                        gmStr = fmtPct(m.gross_margin);
                    } else {
                        gmStr = fmtGrossMargin(m.profit_yi, m.revenue_yi);
                    }
                }
                var curItem = itemName ? (curMetrics[itemName] || {}) : {};
                var prevItem = itemName ? (prevMetrics[itemName] || {}) : {};
                var revYoy = calcGrowth(curItem.revenue_yi, prevItem.revenue_yi);
                var profitYoy = calcGrowth(curItem.profit_yi, prevItem.profit_yi);
                var gmDiff = calcDiff(curItem.gross_margin, prevItem.gross_margin);
                rows.push(
                    '<tr' + (i === 0 ? ' class="mainbz-rank-1-' + secKey + '"' : '') + '>' +
                        (i === 0 ? '<td rowspan="3">' + secLabel + '</td>' : '') +
                        '<td>' + String(i + 1) + '</td>' +
                        '<td class="mainbz-item">' + esc(rr.bz_item) + '</td>' +
                        '<td>' + fmtYi(revYi) + '</td>' +
                        '<td>' + revPct + '</td>' +
                        '<td>' + fmtYi(costYi) + '</td>' +
                        '<td>' + costPct + '</td>' +
                        '<td>' + fmtYi(profitYi) + '</td>' +
                        '<td>' + profitPct + '</td>' +
                        '<td>' + gmStr + '</td>' +
                        (compareSnapshot
                            ? (
                                '<td class="mainbz-delta' + deltaClass(revYoy) + '">' + fmtDelta(revYoy) + '</td>' +
                                '<td class="mainbz-delta' + deltaClass(profitYoy) + '">' + fmtDelta(profitYoy) + '</td>' +
                                '<td class="mainbz-delta' + deltaClass(gmDiff) + '">' + fmtDelta(gmDiff) + '</td>'
                            )
                            : '') +
                    '</tr>'
                );
            }
        }
        pushSectionRows('P', '产品', secP);
        pushSectionRows('I', '行业', secI);
        pushSectionRows('D', '地区', secD);
        return (
            '<div class="fina-fixed-assets-mainbz-panel">' +
                '<table class="fina-fixed-assets-mainbz-table' + (compareSnapshot ? ' mainbz-has-compare' : '') + '">' +
                    '<colgroup>' +
                        '<col class="mainbz-col-type">' +
                        '<col class="mainbz-col-rank">' +
                        '<col class="mainbz-col-item">' +
                        '<col>' +
                        '<col>' +
                        '<col>' +
                        '<col>' +
                        '<col>' +
                        '<col>' +
                        '<col>' +
                        (compareSnapshot ? '<col class="mainbz-col-compare">' : '') +
                        (compareSnapshot ? '<col class="mainbz-col-compare">' : '') +
                        (compareSnapshot ? '<col class="mainbz-col-compare">' : '') +
                    '</colgroup>' +
                    '<thead>' +
                        '<tr>' +
                            '<th colspan="2" class="mainbz-end-cell">' + esc(fmtEndDate(reportEnd)) + '</th>' +
                            '<th>主营构成</th>' +
                            '<th>收入（亿）</th>' +
                            '<th>收入%</th>' +
                            '<th>成本（亿）</th>' +
                            '<th>成本%</th>' +
                            '<th>利润（亿）</th>' +
                            '<th>利润%</th>' +
                            '<th>毛利率%</th>' +
                            (compareSnapshot ? '<th>收入同比</th><th>利润同比</th><th>毛利变化</th>' : '') +
                        '</tr>' +
                    '</thead>' +
                    '<tbody>' + rows.join('') + '</tbody>' +
                '</table>' +
            '</div>'
        );
    }

    var currentSnapshot = p && p.current ? p.current : null;
    var previousSnapshot = p && p.previous ? p.previous : null;
    var leftHtml = '';
    var rightHtml = '';
    if (currentSnapshot) leftHtml = buildSnapshotTable(currentSnapshot, previousSnapshot && previousSnapshot.end_date ? previousSnapshot : null);
    if (previousSnapshot && previousSnapshot.end_date) rightHtml = buildSnapshotTable(previousSnapshot, null);
    if (!leftHtml && p && p.sections) {
        /* 兼容旧接口结构（仅一张表） */
        leftHtml = buildSnapshotTable({ end_date: p.end_date || '', sections: p.sections }, null);
    }
    if (!leftHtml && !rightHtml) {
        el.innerHTML = '<div class="fina-fixed-assets-mainbz-empty">主营构成：暂无数据</div>';
        return;
    }
    var blocks = [];
    if (leftHtml) blocks.push(leftHtml);
    if (rightHtml) blocks.push(rightHtml);
    el.innerHTML = '<div class="fina-fixed-assets-mainbz-dual">' + blocks.join('') + '</div>';
}

/** 与财报表 TTM 区 PE、营收 TTM 及单季列口径一致；payload 来自 /api/fina_fixed_assets_revenue_series；上市时间优先 payload.list_date（与 ts 对齐），否则 arturnRow.list_date */
function renderFinaFixedAssetsMetricsBar(payload, arturnRow) {
    var el = document.getElementById('fina-fixed-assets-modal-metrics');
    if (!el) return;
    if (!payload) {
        el.innerHTML = '';
        return;
    }
    /** 值文本包装：默认蓝色；特殊态（如负值/阈值高亮）可覆盖颜色 */
    function wrapMetricValue(text, extraClass) {
        var cls = 'fina-fixed-assets-metric-v';
        if (extraClass) cls += ' ' + extraClass;
        return '<span class="' + cls + '">' + String(text) + '</span>';
    }
    /** 百分数字符串；负值包一层绿字；阈值高亮金色（同比>30、ROE>5） */
    function fmtPctVal(v, opts) {
        if (v === null || v === undefined || v === '') return '--';
        var n = parseFloat(v);
        if (isNaN(n)) return '--';
        var showPercent = !opts || opts.showPercent !== false;
        var s = n.toFixed(2) + (showPercent ? '%' : '');
        if (opts && typeof opts.highlightAbove === 'number' && n > opts.highlightAbove) {
            return wrapMetricValue(s, 'fina-fixed-assets-metric-hot');
        }
        if (opts && opts.colorNeg && n < 0) {
            return wrapMetricValue(s, 'fina-fixed-assets-metric-neg');
        }
        return wrapMetricValue(s, '');
    }
    var ttm = formatFinaArturnValue(payload.ttm_revenue_yi, 1);
    var pe = formatFinaArturnValue(payload.pe_ttm, 1);
    var anchor = payload.metrics_anchor_date != null ? String(payload.metrics_anchor_date).trim() : '';
    var anchorEsc = anchor.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    var sw2 = arturnRow && arturnRow.sw_name != null ? String(arturnRow.sw_name) : '';
    var totalMvYi = formatFinaArturnValue(arturnRow && arturnRow.total_mv_yi, 1);
    var freeMvYi = formatFinaArturnValue(arturnRow && arturnRow.free_mv_yi, 1);
    var sw2Esc = _finaArturnEscCellText(sw2 || '--');
    var actNameRaw = payload && payload.act_name != null ? String(payload.act_name).trim() : '';
    if (!actNameRaw && arturnRow && arturnRow.act_name != null) {
        actNameRaw = String(arturnRow.act_name).trim();
    }
    var actEntRaw = payload && payload.act_ent_type != null ? String(payload.act_ent_type).trim() : '';
    if (!actEntRaw && arturnRow && arturnRow.act_ent_type != null) {
        actEntRaw = String(arturnRow.act_ent_type).trim();
    }
    var actNameEsc = _finaArturnEscCellText(actNameRaw || '--');
    var actEntEsc = _finaArturnEscCellText(actEntRaw || '--');
    var anchorValueHtml = wrapMetricValue(anchorEsc, '');
    var fcfTtmYi = formatFinaArturnValue(payload.fcf_star_ttm_yi, 2);
    function fmtNetDebtYiBlock() {
        var raw = payload.netdebt_yi;
        if (raw === null || raw === undefined || raw === '') {
            return wrapMetricValue('--', '');
        }
        var n = parseFloat(raw);
        if (isNaN(n)) return wrapMetricValue('--', '');
        var abs = Math.abs(n).toFixed(2);
        if (n < 0) {
            return wrapMetricValue('净现金\u00a0' + abs + '\u00a0亿', 'fina-fixed-assets-metric-netcash');
        }
        if (n > 0) {
            return wrapMetricValue('净债务\u00a0' + abs + '\u00a0亿', '');
        }
        return wrapMetricValue('0', '');
    }
    var sep = '<span class="fina-fixed-assets-metrics-sep">·</span>';
    var rowFund = [];
    if (anchor) {
        rowFund.push(
            '<span class="fina-fixed-assets-metrics-anchor"><span class="fina-fixed-assets-metric-k">报告期</span> ' +
                anchorValueHtml +
                '</span>'
        );
    }
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">营收(TTM)</span>' +
            wrapMetricValue(ttm === '--' ? '--' : ttm + '亿', '') +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">营收同比</span> ' +
            fmtPctVal(payload.q_gr_yoy, { colorNeg: true, highlightAbove: 30, showPercent: true }) +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">营业利润同比</span> ' +
            fmtPctVal(payload.q_op_yoy, { colorNeg: true, highlightAbove: 30, showPercent: true }) +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">毛利率</span> ' +
            fmtPctVal(payload.q_gsprofit_margin, { colorNeg: false, showPercent: true }) +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">净利率</span> ' +
            fmtPctVal(payload.q_netprofit_margin, { colorNeg: true, showPercent: true }) +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">ROE(扣非)</span> ' +
            fmtPctVal(payload.q_dt_roe, { colorNeg: true, highlightAbove: 5, showPercent: false }) +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">FCF*(TTM)</span>' +
            wrapMetricValue(fcfTtmYi === '--' ? '--' : fcfTtmYi + '亿', '') +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">净债务/净现金</span> ' +
            fmtNetDebtYiBlock() +
            '</span>'
    );
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">资产负债率</span> ' +
            fmtPctVal(payload.debt_to_assets, { colorNeg: false, showPercent: true }) +
            '</span>'
    );
    var cccTxt = '--';
    var cccCls = '';
    if (payload.ccc_q !== null && payload.ccc_q !== undefined && payload.ccc_q !== '') {
        var cq = parseFloat(payload.ccc_q);
        if (!isNaN(cq)) {
            cccTxt = cq.toFixed(1) + '\u00a0天';
            if (cq < 0) {
                cccCls = 'fina-fixed-assets-metric-ccc-neg';
            }
        }
    }
    rowFund.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">CCC(最近季)</span> ' +
            wrapMetricValue(cccTxt, cccCls) +
            '</span>'
    );
    var listDateRaw = '';
    if (payload && payload.list_date != null && String(payload.list_date).trim() !== '') {
        listDateRaw = String(payload.list_date).trim();
    } else if (arturnRow && arturnRow.list_date != null && String(arturnRow.list_date).trim() !== '') {
        listDateRaw = String(arturnRow.list_date).trim();
    }
    var listDateDisp = listDateRaw ? formatFinaArturnAnnDate(listDateRaw) : '--';
    var listDateValueHtml = listDateDisp;
    if (listDateRaw && listDateDisp !== '--') {
        var tenureZh = finaListingTenureZhFromNow(listDateRaw);
        if (tenureZh) {
            listDateValueHtml = listDateDisp + '\u00a0' + tenureZh;
        }
    }
    var rowMarket = [];
    rowMarket.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">上市时间</span> ' +
            wrapMetricValue(listDateValueHtml, '') +
            '</span>'
    );
    rowMarket.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">PE</span> ' +
            wrapMetricValue(pe, '') +
            '</span>'
    );
    rowMarket.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">总市值</span> ' +
            wrapMetricValue(totalMvYi === '--' ? '--' : totalMvYi + '亿', '') +
            '</span>'
    );
    rowMarket.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">自由流通市值</span> ' +
            wrapMetricValue(freeMvYi === '--' ? '--' : freeMvYi + '亿', '') +
            '</span>'
    );
    rowMarket.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">申万行业</span> ' +
            wrapMetricValue(sw2Esc, '') +
            '</span>'
    );
    rowMarket.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">实控人</span> ' +
            wrapMetricValue(actNameEsc, '') +
            '</span>'
    );
    rowMarket.push(
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">企业性质</span> ' +
            wrapMetricValue(actEntEsc, '') +
            '</span>'
    );
    el.innerHTML =
        '<div class="fina-fixed-assets-metrics-inner">' +
        '<div class="fina-fixed-assets-metrics-row fina-fixed-assets-metrics-row--fundamentals">' +
            rowFund.join(sep) +
        '</div>' +
        '<div class="fina-fixed-assets-metrics-row fina-fixed-assets-metrics-row--market">' +
            rowMarket.join(sep) +
        '</div>' +
        '</div>';
}

function loadFinaFixedAssetsModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(FINA_FIXED_ASSETS_MODAL_LAYOUT_STORAGE_KEY);
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

function saveFinaFixedAssetsModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(
            FINA_FIXED_ASSETS_MODAL_LAYOUT_STORAGE_KEY,
            JSON.stringify({ w: w, h: h, l: l, t: t })
        );
    } catch (e) {
        /* ignore */
    }
}

function applyFinaFixedAssetsModalSavedLayout() {
    var overlay = document.getElementById('fina-fixed-assets-modal-overlay');
    var modal = overlay && overlay.querySelector('.fina-fixed-assets-modal-shell');
    if (!modal) return;
    var lay = loadFinaFixedAssetsModalLayout();
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

function scheduleSaveFinaFixedAssetsModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(finaFixedAssetsModalSaveTimer);
    finaFixedAssetsModalSaveTimer = setTimeout(function () {
        if (Date.now() - finaFixedAssetsModalLayoutOpenTs < 700) return;
        saveFinaFixedAssetsModalLayout(modalEl);
    }, 400);
}

function loadFinaCashCreateModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(FINA_CASH_CREATE_MODAL_LAYOUT_STORAGE_KEY);
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

function saveFinaCashCreateModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(
            FINA_CASH_CREATE_MODAL_LAYOUT_STORAGE_KEY,
            JSON.stringify({ w: w, h: h, l: l, t: t })
        );
    } catch (e) {
        /* ignore */
    }
}

function applyFinaCashCreateModalSavedLayout() {
    var overlay = document.getElementById('fina-cash-create-modal-overlay');
    var modal = overlay && overlay.querySelector('.fina-cash-create-modal-shell');
    if (!modal) return;
    var lay = loadFinaCashCreateModalLayout();
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

function scheduleSaveFinaCashCreateModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(finaCashCreateModalSaveTimer);
    finaCashCreateModalSaveTimer = setTimeout(function () {
        if (Date.now() - finaCashCreateModalLayoutOpenTs < 700) return;
        saveFinaCashCreateModalLayout(modalEl);
    }, 400);
}

function loadFinaWorkingCapModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(FINA_WORKING_CAP_MODAL_LAYOUT_STORAGE_KEY);
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

function saveFinaWorkingCapModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(
            FINA_WORKING_CAP_MODAL_LAYOUT_STORAGE_KEY,
            JSON.stringify({ w: w, h: h, l: l, t: t })
        );
    } catch (e) {
        /* ignore */
    }
}

function applyFinaWorkingCapModalSavedLayout() {
    var overlay = document.getElementById('fina-working-cap-modal-overlay');
    var modal = overlay && overlay.querySelector('.fina-working-cap-modal-shell');
    if (!modal) return;
    var lay = loadFinaWorkingCapModalLayout();
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

function scheduleSaveFinaWorkingCapModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(finaWorkingCapModalSaveTimer);
    finaWorkingCapModalSaveTimer = setTimeout(function () {
        if (Date.now() - finaWorkingCapModalLayoutOpenTs < 700) return;
        saveFinaWorkingCapModalLayout(modalEl);
    }, 400);
}

function loadFinaCapEffModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(FINA_CAP_EFF_MODAL_LAYOUT_STORAGE_KEY);
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

function saveFinaCapEffModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 320 || h < 320) return;
        localStorage.setItem(
            FINA_CAP_EFF_MODAL_LAYOUT_STORAGE_KEY,
            JSON.stringify({ w: w, h: h, l: l, t: t })
        );
    } catch (e) {
        /* ignore */
    }
}

function applyFinaCapEffModalSavedLayout() {
    var overlay = document.getElementById('fina-cap-eff-modal-overlay');
    var modal = overlay && overlay.querySelector('.fina-cap-eff-modal-shell');
    if (!modal) return;
    var lay = loadFinaCapEffModalLayout();
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

function scheduleSaveFinaCapEffModalLayout(modalEl) {
    if (!modalEl) return;
    clearTimeout(finaCapEffModalSaveTimer);
    finaCapEffModalSaveTimer = setTimeout(function () {
        if (Date.now() - finaCapEffModalLayoutOpenTs < 700) return;
        saveFinaCapEffModalLayout(modalEl);
    }, 400);
}

function loadFinaSchemeModalLayout() {
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(FINA_SCHEME_MODAL_LAYOUT_STORAGE_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        if (typeof o.w !== 'number' || typeof o.h !== 'number') return null;
        if (o.w < 200 || o.h < 120) return null;
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

function saveFinaSchemeModalLayout(modalEl) {
    try {
        if (!modalEl || typeof localStorage === 'undefined') return;
        var rect = modalEl.getBoundingClientRect();
        var w = Math.round(rect.width);
        var h = Math.round(rect.height);
        var l = Math.round(rect.left);
        var t = Math.round(rect.top);
        if (w < 200 || h < 120) return;
        finaSchemeModalLayoutMemory = { w: w, h: h, l: l, t: t };
        localStorage.setItem(
            FINA_SCHEME_MODAL_LAYOUT_STORAGE_KEY,
            JSON.stringify({ w: w, h: h, l: l, t: t })
        );
    } catch (e) {
        /* ignore */
    }
}

/** 空闲时写 localStorage，避免关窗点击与磁盘同步竞争主线程 */
function persistFinaSchemeModalLayoutSnapAsync(snap) {
    if (!snap || snap.w < 200 || snap.h < 120) return;
    try {
        var ric = window.requestIdleCallback;
        var run = function () {
            try {
                if (typeof localStorage === 'undefined') return;
                localStorage.setItem(
                    FINA_SCHEME_MODAL_LAYOUT_STORAGE_KEY,
                    JSON.stringify({ w: snap.w, h: snap.h, l: snap.l, t: snap.t })
                );
            } catch (e) {
                /* ignore */
            }
        };
        if (typeof ric === 'function') {
            ric(run, { timeout: 2000 });
        } else {
            setTimeout(run, 0);
        }
    } catch (e) {
        /* ignore */
    }
}

function applyFinaSchemeModalSavedLayout() {
    var overlay = document.getElementById('fina-arturn-scheme-modal-overlay');
    var modal = overlay && overlay.querySelector('.fina-arturn-scheme-modal-shell');
    if (!modal) return;
    var lay = loadFinaSchemeModalLayout();
    if (!lay) {
        /* 无存盘布局时下一帧测一次几何写入 memory，关「开即关」时仍可能回落到 close 内读 rect */
        requestAnimationFrame(function () {
            try {
                var rect = modal.getBoundingClientRect();
                var w = Math.round(rect.width);
                var h = Math.round(rect.height);
                if (w < 200 || h < 120) return;
                finaSchemeModalLayoutMemory = {
                    w: w,
                    h: h,
                    l: Math.round(rect.left),
                    t: Math.round(rect.top)
                };
            } catch (e) {
                /* ignore */
            }
        });
        return;
    }
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var minW = 200;
    var minH = 120;
    var w = Math.min(Math.max(lay.w, minW), Math.max(minW, vw - 8));
    var h = Math.min(Math.max(lay.h, minH), Math.max(minH, vh - 8));
    modal.style.width = w + 'px';
    modal.style.height = h + 'px';
    modal.style.transform = 'none';
    var l = lay.l != null ? lay.l : (vw - w) / 2;
    var t = lay.t != null ? lay.t : (vh - h) / 2;
    l = Math.max(0, Math.min(l, vw - w));
    t = Math.max(0, Math.min(t, vh - h));
    modal.style.left = l + 'px';
    modal.style.top = t + 'px';
    finaSchemeModalLayoutMemory = {
        w: Math.round(w),
        h: Math.round(h),
        l: Math.round(l),
        t: Math.round(t)
    };
}

function scheduleSaveFinaSchemeModalLayout(modalEl) {
    if (!modalEl) return;
    var ol = document.getElementById('fina-arturn-scheme-modal-overlay');
    if (!ol || !ol.classList.contains('is-open')) return;
    clearTimeout(finaSchemeModalSaveTimer);
    finaSchemeModalSaveTimer = setTimeout(function () {
        if (Date.now() - finaSchemeModalLayoutOpenTs < 700) return;
        var ol2 = document.getElementById('fina-arturn-scheme-modal-overlay');
        if (!ol2 || !ol2.classList.contains('is-open')) return;
        saveFinaSchemeModalLayout(modalEl);
    }, 400);
}

function closeFinaFixedAssetsModal() {
    var overlay = document.getElementById('fina-fixed-assets-modal-overlay');
    if (!overlay) return;
    var wasOpen = overlay.classList.contains('is-open');
    var modal = overlay.querySelector('.fina-fixed-assets-modal-shell');
    if (wasOpen && modal) saveFinaFixedAssetsModalLayout(modal);
    setFinaFixedAssetsGrowthConfigPanelOpen(false);
    closeFinaCashCreateModal();
    closeFinaWorkingCapModal();
    closeFinaCapEffModal();
    overlay.classList.remove('is-open');
    showFinaFixedAssetsMsg('');
    var secEl = document.getElementById('fina-fixed-assets-modal-sec-name');
    var topicEl = document.getElementById('fina-fixed-assets-modal-topic');
    if (secEl) secEl.textContent = '';
    if (topicEl) {
        topicEl.textContent = FINA_FIXED_ASSETS_HEADER_CHART_TITLE;
        topicEl.removeAttribute('title');
    }
    var centerEl = document.querySelector('.fina-fixed-assets-header-center');
    if (centerEl) centerEl.removeAttribute('title');
    clearFinaFixedAssetsMetricsBar();
    clearFinaFixedAssetsMainbzTable();
    if (finaFixedAssetsChartInstance) {
        finaFixedAssetsChartInstance.destroy();
        finaFixedAssetsChartInstance = null;
    }
    clearFinaFixedAssetsOperatingDataTable();
    finaFixedAssetsCurrentRows = [];
    finaFixedAssetsCurrentTitle = '';
    if (wasOpen) _modalClosed();
}

function clearFinaFixedAssetsOperatingDataTable() {
    var wrap = document.getElementById('fina-fixed-assets-operating-data-wrap');
    if (wrap) wrap.innerHTML = '';
}

function loadFinaFixedAssetsGrowthConfig() {
    var cfg = {};
    FINA_FIXED_ASSETS_GROWTH_METRIC_OPTIONS.forEach(function (opt) {
        cfg[opt.key] = true;
    });
    try {
        if (typeof localStorage === 'undefined') return cfg;
        var raw = localStorage.getItem(FINA_FIXED_ASSETS_GROWTH_CONFIG_STORAGE_KEY);
        if (!raw) return cfg;
        var saved = JSON.parse(raw);
        FINA_FIXED_ASSETS_GROWTH_METRIC_OPTIONS.forEach(function (opt) {
            if (saved && Object.prototype.hasOwnProperty.call(saved, opt.key)) {
                cfg[opt.key] = saved[opt.key] !== false;
            }
        });
    } catch (e) {
        return cfg;
    }
    return cfg;
}

function saveFinaFixedAssetsGrowthConfig(cfg) {
    try {
        if (typeof localStorage === 'undefined') return;
        localStorage.setItem(FINA_FIXED_ASSETS_GROWTH_CONFIG_STORAGE_KEY, JSON.stringify(cfg || {}));
    } catch (e) {
        /* ignore */
    }
}

function renderFinaFixedAssetsGrowthConfigPanel() {
    var panel = document.getElementById('fina-fixed-assets-growth-config-panel');
    if (!panel) return;
    var cfg = loadFinaFixedAssetsGrowthConfig();
    var rows = FINA_FIXED_ASSETS_GROWTH_METRIC_OPTIONS.map(function (opt) {
        var checked = cfg[opt.key] !== false ? ' checked' : '';
        return (
            '<label class="fina-fixed-assets-growth-config-line">' +
            '<input type="checkbox" data-growth-key="' +
            opt.key +
            '"' +
            checked +
            '> ' +
            _finaArturnEscCellText(opt.label) +
            '</label>'
        );
    }).join('');
    panel.innerHTML =
        '<div class="fina-fixed-assets-growth-config-title">成长性显示指标</div>' +
        rows +
        '<div class="fina-fixed-assets-growth-config-hint">设置会自动保存</div>';
}

function setFinaFixedAssetsGrowthConfigPanelOpen(open) {
    var btn = document.getElementById('fina-fixed-assets-growth-config-btn');
    var panel = document.getElementById('fina-fixed-assets-growth-config-panel');
    if (!btn || !panel) return;
    if (open) {
        renderFinaFixedAssetsGrowthConfigPanel();
        panel.hidden = false;
        btn.setAttribute('aria-expanded', 'true');
    } else {
        panel.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
    }
}

function applyFinaFixedAssetsGrowthConfigFromPanel() {
    var panel = document.getElementById('fina-fixed-assets-growth-config-panel');
    if (!panel) return;
    var cfg = loadFinaFixedAssetsGrowthConfig();
    var inputs = panel.querySelectorAll('input[data-growth-key]');
    inputs.forEach(function (input) {
        var key = input.getAttribute('data-growth-key');
        if (key) cfg[key] = !!input.checked;
    });
    saveFinaFixedAssetsGrowthConfig(cfg);
    if (Array.isArray(finaFixedAssetsCurrentRows) && finaFixedAssetsCurrentRows.length > 0) {
        renderFinaFixedAssetsOperatingDataTable(finaFixedAssetsCurrentRows);
    }
}

/**
 * 固定资产弹窗下方经营宽表：列为报告期（旧→新），右起「指标」「业务逻辑」两列 sticky。
 * 首四行（经营现金流净额、资本开支、FCF、现金转化率）在「业务逻辑」列 rowspan=4 合并为「现金创造能力」（行头不再缀「模块A·」）。
 * 模块B 五行（应收/存货/应付天数、现金循环天数、应收占收入比）：业务逻辑 rowspan=5，「营运资本」+换行+「周转天数」。模块C 三行：「资本结构」+换行+「杠杆」。
 * 其下依次为：资本效率(2)→规模.损益(3)→盈利能力(2)→成长性(可配置)→股东与筹码(1)；行序见 specs 与 tail*Logic* 常量（勿只改 specs 不改跨度）。
 * 应收天数·存货天数·应收占收入比：仍参与 max/min/median（极值三色；均为越小越好）；仅当该格未标极值时，再按相对行中位数标浅红/浅绿。
 */
function renderFinaFixedAssetsOperatingDataTable(rows) {
    var wrap = document.getElementById('fina-fixed-assets-operating-data-wrap');
    if (!wrap) return;
    if (!Array.isArray(rows) || rows.length === 0) {
        wrap.innerHTML = '';
        return;
    }
    function escCellText(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
    /** meanTone：应收/存货/应收占收入比在非 max/min/median 格上叠加相对行中位数（低于浅红、高于浅绿）；极值三色优先 */
    function tdNumStyled(
        rawVal,
        band,
        decPlaces,
        yoyOver30,
        roeOver4,
        cccNeg,
        netdebtNeg,
        meanTone
    ) {
        var cls = 'fina-fa-opdata-num';
        if (band === 'max') cls += ' fina-fa-opdata-cell-max';
        else if (band === 'min') cls += ' fina-fa-opdata-cell-min';
        else if (band === 'median') cls += ' fina-fa-opdata-cell-median';
        else {
            if (meanTone === 'below') cls += ' fina-fa-opdata-mean-below';
            else if (meanTone === 'above') cls += ' fina-fa-opdata-mean-above';
        }
        if (yoyOver30) cls += ' fina-fa-opdata-yoy-over-30';
        if (roeOver4) cls += ' fina-fa-opdata-roep-over-4';
        if (cccNeg) cls += ' fina-fa-opdata-ccc-neg';
        if (netdebtNeg) cls += ' fina-fa-opdata-netdebt-neg';
        if (rawVal == null || rawVal === '') return '<td class="' + cls + '">--</td>';
        var n = parseFloat(rawVal);
        if (isNaN(n)) return '<td class="' + cls + '">--</td>';
        if (n < 0) cls += ' fina-fa-opdata-negative';
        var d =
            typeof decPlaces === 'number' && decPlaces >= 0 ? Math.min(12, Math.floor(decPlaces)) : 2;
        return '<td class="' + cls + '">' + n.toFixed(d) + '</td>';
    }
    var colLabels = rows.map(function (r) {
        return formatFinaQuarterLabel((r || {}).end_date);
    });
    var headCols = colLabels
        .map(function (q) {
            return '<th scope="col" class="fina-fa-opdata-col">' + escCellText(q) + '</th>';
        })
        .join('');
    var specs = [
        { label: '经营现金流净额（亿）', key: 'q_ocf_yi', decimals: 2 },
        { label: '资本开支（亿）', key: 'q_capex_yi', decimals: 2 },
        { label: '自由现金流FCF（亿）', key: 'q_fcf_star_yi', decimals: 2 },
        { label: '现金转化率', key: 'q_ocf_to_or_pct', decimals: 2 },
        { label: '应收天数（天）', key: 'ar_days_q', decimals: 1 },
        { label: '存货天数（天）', key: 'inv_days_q', decimals: 1 },
        { label: '应付天数（天）', key: 'ap_days_q', decimals: 1 },
        { label: '现金循环天数（天）', key: 'ccc_q', decimals: 1 },
        { label: '应收占收入比', key: 'recv_pct', decimals: 2 },
        { label: '资产负债率（%）', key: 'debt_to_assets_q', decimals: 2 },
        { label: '带息负债率（%）', key: 'interestdebt_ratio_q', decimals: 2 },
        { label: '净债务/净现金（亿）', key: 'netdebt_yi_q', decimals: 1 },
        {
            label: 'ROIC',
            key: 'roic_sq_pct_avg_ic_q',
            decimals: 2,
        },
        { label: 'ROE(扣非)', key: 'q_dt_roe', decimals: 2 },
        { label: '营业收入（亿）', key: 'q_revenue_yi', decimals: 1 },
        { label: '经营活动净收益（亿）', key: 'q_opincome_yi', decimals: 1 },
        { label: '扣非净利润（亿）', key: 'q_dtprofit_yi', decimals: 1 },
        { label: '毛利率(%)', key: 'q_gsprofit_margin', decimals: 2 },
        { label: '净利率(%)', key: 'q_netprofit_margin', decimals: 2 },
        { label: '营业收入同比(%)', key: 'q_gr_yoy', decimals: 2 },
        { label: '营业收入环比(%)', key: 'q_gr_qoq', decimals: 2 },
        { label: '营业利润同比(%)', key: 'q_op_yoy', decimals: 2 },
        { label: '营业利润环比(%)', key: 'q_op_qoq', decimals: 2 },
        { label: '归母净利同比(%)', key: 'q_netprofit_yoy', decimals: 2 },
        { label: '归母净利环比(%)', key: 'q_netprofit_qoq', decimals: 2 },
        { label: '股东户数(万户)', key: 'holder_num', decimals: 2 },
    ];
    var growthMetricVisible = loadFinaFixedAssetsGrowthConfig();
    var growthMetricKeys = {};
    FINA_FIXED_ASSETS_GROWTH_METRIC_OPTIONS.forEach(function (opt) {
        growthMetricKeys[opt.key] = 1;
    });
    specs = specs.filter(function (sp) {
        if (!growthMetricKeys[sp.key]) return true;
        return growthMetricVisible[sp.key] !== false;
    });
    // 默认大为优（goodCol→band max、badCol→band min 对应稿表玫红/青绿）；以下指标小为优（与应收天数同：最低值列走 band max、最高值列走 band min）
    var lowerIsBetterKeys = {
        ar_days_q: 1,
        inv_days_q: 1,
        ccc_q: 1,
        recv_pct: 1,
        debt_to_assets_q: 1,
        interestdebt_ratio_q: 1,
        netdebt_yi_q: 1,
    };
    /** 有效数值≥3：奇数取排序正中元素对应值，偶数取中间两数平均；仅标一列（并列取最左列）。 */
    function rowMedianHighlightCol(parsed) {
        var finiteList = parsed.filter(function (x) {
            return x !== null;
        });
        var n = finiteList.length;
        if (n < 3) return -1;
        var sorted = finiteList.slice().sort(function (a, b) {
            return a - b;
        });
        var idx;
        if (n % 2 === 1) {
            var medVal = sorted[(n - 1) / 2];
            for (idx = 0; idx < parsed.length; idx += 1) {
                var vi = parsed[idx];
                if (vi !== null && vi === medVal) return idx;
            }
            return -1;
        }
        var medInterp = (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
        var minDist = Infinity;
        for (idx = 0; idx < parsed.length; idx += 1) {
            var vj = parsed[idx];
            if (vj === null) continue;
            var d = Math.abs(vj - medInterp);
            if (d < minDist) minDist = d;
        }
        if (minDist === Infinity) return -1;
        var eps = 1e-9;
        for (idx = 0; idx < parsed.length; idx += 1) {
            var vk = parsed[idx];
            if (vk === null) continue;
            if (Math.abs(Math.abs(vk - medInterp) - minDist) < eps) return idx;
        }
        return -1;
    }
    /** 行内有限值的中位数（奇数取正中，偶数取中间两数平均）；用于应收/存货/应收占收入比的偏离浅色底 */
    function rowMedianRefValue(finiteList) {
        if (!finiteList || finiteList.length === 0) return null;
        var sorted = finiteList.slice().sort(function (a, b) {
            return a - b;
        });
        var n = sorted.length;
        if (n % 2 === 1) return sorted[(n - 1) / 2];
        return (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
    }
    var moduleALogicRowspan = 4;
    var moduleALogicLabel = '现金创造能力';
    /** 与 specs 中模块B（应收/存货/应付天数、现金循环天数、应收占收入比）五行顺序一致 */
    var moduleBLogicRowspan = 5;
    var moduleBLogicStartRow = moduleALogicRowspan;
    /** 与 specs 中模块C（资产负债率/带息负债率/净债务）三行顺序一致 */
    var moduleCLogicRowspan = 3;
    var moduleCLogicStartRow = moduleBLogicStartRow + moduleBLogicRowspan;
    /** 模块C 之后：资本效率 / 规模·损益 / 盈利能力 / 成长性 / 股东与筹码（与 specs 顺序一致） */
    var afterModuleCRow = moduleCLogicStartRow + moduleCLogicRowspan;
    var tailD1LogicRowspan = 2;
    var tailD1LogicStartRow = afterModuleCRow;
    var tailD2LogicRowspan = 3;
    var tailD2LogicStartRow = tailD1LogicStartRow + tailD1LogicRowspan;
    var tailD3LogicRowspan = 2;
    var tailD3LogicStartRow = tailD2LogicStartRow + tailD2LogicRowspan;
    var tailD4LogicRowspan = 0;
    FINA_FIXED_ASSETS_GROWTH_METRIC_OPTIONS.forEach(function (opt) {
        if (growthMetricVisible[opt.key] !== false) tailD4LogicRowspan += 1;
    });
    var tailD4LogicStartRow = tailD3LogicStartRow + tailD3LogicRowspan;
    var tailD5LogicRowspan = 1;
    var tailD5LogicStartRow = tailD4LogicStartRow + tailD4LogicRowspan;
    var bodyRows = specs
        .map(function (sp, rowIdx) {
            var parsed = rows.map(function (r) {
                var raw = (r || {})[sp.key];
                if (raw == null || raw === '') return null;
                var n = parseFloat(raw);
                if (isNaN(n)) return null;
                if (sp.key === 'holder_num') n = n / 10000;
                return n;
            });
            var finiteList = parsed.filter(function (x) {
                return x !== null;
            });
            var rowMin = finiteList.length ? Math.min.apply(null, finiteList) : null;
            var rowMax = finiteList.length ? Math.max.apply(null, finiteList) : null;
            var maxCol = -1;
            var minCol = -1;
            var ci;
            if (rowMax !== null) {
                for (ci = 0; ci < parsed.length; ci += 1) {
                    if (parsed[ci] !== null && parsed[ci] === rowMax) {
                        maxCol = ci;
                        break;
                    }
                }
            }
            if (rowMin !== null) {
                for (ci = 0; ci < parsed.length; ci += 1) {
                    if (parsed[ci] !== null && parsed[ci] === rowMin) {
                        minCol = ci;
                        break;
                    }
                }
            }
            var preferLower = !!lowerIsBetterKeys[sp.key];
            var goodCol = preferLower ? minCol : maxCol;
            var badCol = preferLower ? maxCol : minCol;
            var medianCol = rowMedianHighlightCol(parsed);
            var isArInvRecvMedianToneRow =
                sp.key === 'ar_days_q' ||
                sp.key === 'inv_days_q' ||
                sp.key === 'recv_pct';
            var rowMedianRefVal =
                isArInvRecvMedianToneRow && finiteList.length > 0
                    ? rowMedianRefValue(finiteList)
                    : null;
            var cells = parsed
                .map(function (ni, colIdx) {
                    if (ni === null) {
                        var dec0 =
                            typeof sp.decimals === 'number' ? sp.decimals : 2;
                        return tdNumStyled(null, '', dec0, false, false, false, false, null);
                    }
                    var dec =
                        typeof sp.decimals === 'number' ? sp.decimals : 2;
                    var band = '';
                    if (finiteList.length && rowMin !== null && rowMax !== null) {
                        if (colIdx === goodCol) band = 'max';
                        else if (colIdx === badCol) band = 'min';
                        else if (medianCol >= 0 && colIdx === medianCol) band = 'median';
                    }
                    var meanTone = null;
                    if (
                        isArInvRecvMedianToneRow &&
                        !band &&
                        rowMedianRefVal !== null &&
                        finiteList.length > 0
                    ) {
                        var epsMed = 1e-6 * (Math.abs(rowMedianRefVal) + 1);
                        if (ni < rowMedianRefVal - epsMed) meanTone = 'below';
                        else if (ni > rowMedianRefVal + epsMed) meanTone = 'above';
                    }
                    var yoyHi =
                        (
                            sp.key === 'q_gr_yoy' ||
                            sp.key === 'q_gr_qoq' ||
                            sp.key === 'q_op_yoy' ||
                            sp.key === 'q_op_qoq' ||
                            sp.key === 'q_netprofit_yoy' ||
                            sp.key === 'q_netprofit_qoq'
                        ) &&
                        ni > 30 &&
                        band === '' &&
                        !meanTone;
                    var roeHi =
                        sp.key === 'q_dt_roe' && ni > 4 && band === '' && !meanTone;
                    var cccNeg = sp.key === 'ccc_q' && ni < 0;
                    var netdebtNeg = sp.key === 'netdebt_yi_q' && ni < 0;
                    return tdNumStyled(ni, band, dec, yoyHi, roeHi, cccNeg, netdebtNeg, meanTone);
                })
                .join('');
            var logicCell = '';
            if (rowIdx === 0) {
                logicCell =
                    '<td rowspan="' +
                    moduleALogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-cash-merge fina-fa-opdata-logic-clickable" ' +
                    'title="点击查看现金创造能力 25 季图表">' +
                    escCellText(moduleALogicLabel) +
                    '</td>';
            } else if (rowIdx < moduleALogicRowspan) {
                logicCell = '';
            } else if (rowIdx === moduleBLogicStartRow) {
                logicCell =
                    '<td rowspan="' +
                    moduleBLogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-module-b-merge fina-fa-opdata-workingcap-merge fina-fa-opdata-logic-clickable"' +
                    ' title="点击查看营运资本·周转天数 25 季图表">' +
                    escCellText('营运资本') +
                    '<br>' +
                    escCellText('周转天数') +
                    '</td>';
            } else if (rowIdx < moduleBLogicStartRow + moduleBLogicRowspan) {
                logicCell = '';
            } else if (rowIdx === moduleCLogicStartRow) {
                logicCell =
                    '<td rowspan="' +
                    moduleCLogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-module-c-merge">' +
                    escCellText('资本结构') +
                    '<br>' +
                    escCellText('杠杆') +
                    '</td>';
            } else if (rowIdx < moduleCLogicStartRow + moduleCLogicRowspan) {
                logicCell = '';
            } else if (rowIdx === tailD1LogicStartRow) {
                logicCell =
                    '<td rowspan="' +
                    tailD1LogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-module-b-merge fina-fa-opdata-capeff-merge fina-fa-opdata-logic-clickable" title="点击查看资本效率 25 季图表">' +
                    escCellText('资本效率') +
                    '</td>';
            } else if (rowIdx < tailD1LogicStartRow + tailD1LogicRowspan) {
                logicCell = '';
            } else if (rowIdx === tailD2LogicStartRow) {
                logicCell =
                    '<td rowspan="' +
                    tailD2LogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-module-c-merge">' +
                    escCellText('规模.损益') +
                    '</td>';
            } else if (rowIdx < tailD2LogicStartRow + tailD2LogicRowspan) {
                logicCell = '';
            } else if (rowIdx === tailD3LogicStartRow) {
                logicCell =
                    '<td rowspan="' +
                    tailD3LogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-module-b-merge">' +
                    escCellText('盈利能力') +
                    '</td>';
            } else if (rowIdx < tailD3LogicStartRow + tailD3LogicRowspan) {
                logicCell = '';
            } else if (tailD4LogicRowspan > 0 && rowIdx === tailD4LogicStartRow) {
                logicCell =
                    '<td rowspan="' +
                    tailD4LogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-module-c-merge">' +
                    escCellText('成长性') +
                    '</td>';
            } else if (
                tailD4LogicRowspan > 0 &&
                rowIdx < tailD4LogicStartRow + tailD4LogicRowspan
            ) {
                logicCell = '';
            } else if (rowIdx === tailD5LogicStartRow) {
                logicCell =
                    '<td rowspan="' +
                    tailD5LogicRowspan +
                    '" class="fina-fa-opdata-logic-merge fina-fa-opdata-module-b-merge">' +
                    escCellText('股东与筹码') +
                    '</td>';
            } else {
                logicCell = '<td class="fina-fa-opdata-logic-cell"></td>';
            }
            var frameRowClass = '';
            if (rowIdx < moduleALogicRowspan) {
                frameRowClass = 'fina-fa-opdata-cash-row';
                if (rowIdx === 0) frameRowClass += ' fina-fa-opdata-cash-row-start';
                if (rowIdx === moduleALogicRowspan - 1) frameRowClass += ' fina-fa-opdata-cash-row-end';
            } else if (
                rowIdx >= moduleBLogicStartRow &&
                rowIdx < moduleBLogicStartRow + moduleBLogicRowspan
            ) {
                frameRowClass = 'fina-fa-opdata-module-b-row';
                if (rowIdx === moduleBLogicStartRow) frameRowClass += ' fina-fa-opdata-module-b-row-start';
                if (rowIdx === moduleBLogicStartRow + moduleBLogicRowspan - 1) {
                    frameRowClass += ' fina-fa-opdata-module-b-row-end';
                }
            } else if (
                rowIdx >= moduleCLogicStartRow &&
                rowIdx < moduleCLogicStartRow + moduleCLogicRowspan
            ) {
                frameRowClass = 'fina-fa-opdata-module-c-row';
                if (rowIdx === moduleCLogicStartRow) frameRowClass += ' fina-fa-opdata-module-c-row-start';
                if (rowIdx === moduleCLogicStartRow + moduleCLogicRowspan - 1) {
                    frameRowClass += ' fina-fa-opdata-module-c-row-end';
                }
            } else if (
                rowIdx >= tailD1LogicStartRow &&
                rowIdx < tailD1LogicStartRow + tailD1LogicRowspan
            ) {
                frameRowClass = 'fina-fa-opdata-module-b-row';
                if (rowIdx === tailD1LogicStartRow) frameRowClass += ' fina-fa-opdata-module-b-row-start';
                if (rowIdx === tailD1LogicStartRow + tailD1LogicRowspan - 1) {
                    frameRowClass += ' fina-fa-opdata-module-b-row-end';
                }
            } else if (
                rowIdx >= tailD2LogicStartRow &&
                rowIdx < tailD2LogicStartRow + tailD2LogicRowspan
            ) {
                frameRowClass = 'fina-fa-opdata-module-c-row';
                if (rowIdx === tailD2LogicStartRow) frameRowClass += ' fina-fa-opdata-module-c-row-start';
                if (rowIdx === tailD2LogicStartRow + tailD2LogicRowspan - 1) {
                    frameRowClass += ' fina-fa-opdata-module-c-row-end';
                }
            } else if (
                rowIdx >= tailD3LogicStartRow &&
                rowIdx < tailD3LogicStartRow + tailD3LogicRowspan
            ) {
                frameRowClass = 'fina-fa-opdata-module-b-row';
                if (rowIdx === tailD3LogicStartRow) frameRowClass += ' fina-fa-opdata-module-b-row-start';
                if (rowIdx === tailD3LogicStartRow + tailD3LogicRowspan - 1) {
                    frameRowClass += ' fina-fa-opdata-module-b-row-end';
                }
            } else if (
                rowIdx >= tailD4LogicStartRow &&
                rowIdx < tailD4LogicStartRow + tailD4LogicRowspan
            ) {
                frameRowClass = 'fina-fa-opdata-module-c-row';
                if (rowIdx === tailD4LogicStartRow) frameRowClass += ' fina-fa-opdata-module-c-row-start';
                if (rowIdx === tailD4LogicStartRow + tailD4LogicRowspan - 1) {
                    frameRowClass += ' fina-fa-opdata-module-c-row-end';
                }
            } else if (
                rowIdx >= tailD5LogicStartRow &&
                rowIdx < tailD5LogicStartRow + tailD5LogicRowspan
            ) {
                frameRowClass = 'fina-fa-opdata-module-b-row';
                if (rowIdx === tailD5LogicStartRow) frameRowClass += ' fina-fa-opdata-module-b-row-start';
                if (rowIdx === tailD5LogicStartRow + tailD5LogicRowspan - 1) {
                    frameRowClass += ' fina-fa-opdata-module-b-row-end';
                }
            }
            var trTag = frameRowClass ? '<tr class="' + frameRowClass + '">' : '<tr>';
            return (
                trTag +
                cells +
                '<th scope="row" class="fina-fa-opdata-rowhead">' +
                escCellText(sp.label) +
                '</th>' +
                logicCell +
                '</tr>'
            );
        })
        .join('');
    wrap.innerHTML =
        '<div class="fina-fa-opdata-scroll">' +
        '<table class="fina-fa-opdata-table">' +
        '<thead><tr>' +
        headCols +
        '<th scope="col" class="fina-fa-opdata-corner">指标</th>' +
        '<th scope="col" class="fina-fa-opdata-logic-head">业务逻辑</th></tr></thead>' +
        '<tbody>' +
        bodyRows +
        '</tbody></table></div>';
}

function renderFinaFixedAssetsChart(rows, tsCode) {
    var canvas = document.getElementById('fina-fixed-assets-chart');
    if (!canvas) return;
    if (!window.Chart) {
        showFinaFixedAssetsMsg('图表组件未加载，请 Ctrl+F5 后重试。');
        clearFinaFixedAssetsOperatingDataTable();
        return;
    }
    if (!Array.isArray(rows) || rows.length === 0) {
        showFinaFixedAssetsMsg('暂无可用季度数据');
        clearFinaFixedAssetsOperatingDataTable();
        return;
    }
    var labels = [];
    var fixAssets = [];
    var cipValues = [];
    var inventories = [];
    var qRevenue = [];
    var contractLiab = [];
    for (var i = 0; i < rows.length; i++) {
        var r = rows[i] || {};
        labels.push(formatFinaQuarterLabel(r.end_date));
        var f = parseFloat(r.fix_assets_yi);
        var p = parseFloat(r.cip_yi);
        var iv = parseFloat(r.inventories_yi);
        var q = parseFloat(r.q_revenue_yi);
        var c = parseFloat(r.contract_liab_yi);
        fixAssets.push(isNaN(f) ? null : f);
        cipValues.push(isNaN(p) ? null : p);
        inventories.push(isNaN(iv) ? null : iv);
        qRevenue.push(isNaN(q) ? null : q);
        contractLiab.push(isNaN(c) ? null : c);
    }
    var textPreset = getFinaFixedAssetsChartTextPreset(labels.length);
    if (finaFixedAssetsChartInstance) {
        finaFixedAssetsChartInstance.destroy();
        finaFixedAssetsChartInstance = null;
    }
    var pal = FINA_FIXED_ASSETS_DUAL_PAL;
    var faPal = {
        fixFill: 'rgba(37, 99, 235, 0.88)',
        fixBorder: 'rgba(29, 78, 216, 1)',
        revFill: 'rgba(147, 51, 234, 0.82)',
        revBorder: 'rgba(126, 34, 206, 1)',
        refFill: 'rgba(6, 182, 212, 0.86)',
        refBorder: 'rgba(14, 116, 144, 1)',
        cipFill: 'rgba(16, 185, 129, 0.82)',
        cipBorder: 'rgba(5, 150, 105, 1)',
        invFill: 'rgba(245, 158, 11, 0.9)',
        invBorder: 'rgba(180, 83, 9, 1)',
        lineMain: pal.contractLine,
        lineFillSoft: pal.contractFillSoft,
        lineGlow: pal.contractGlow,
        lblFix: '#1d4ed8',
        lblCip: '#047857',
        lblInv: '#b45309',
        lblRev: '#7e22ce',
        lblRef: '#0e7490',
        lblLine: '#be185d',
    };
    var qRevenuePurple = faPal.revFill;
    var qRevenueRefFill = faPal.refFill;
    var qRevenueRefBorder = faPal.refBorder;
    var qRevenueBarBorderDefault = faPal.revBorder;
    var qRevenueRefIndex = labels.length - 5;
    var qRevenueBgColors = [];
    var qRevenueBorderColors = [];
    var qRevenueBorderWidths = [];
    for (var bi = 0; bi < qRevenue.length; bi += 1) {
        var isRefBar = bi === qRevenueRefIndex && qRevenueRefIndex >= 0;
        qRevenueBgColors.push(isRefBar ? qRevenueRefFill : qRevenuePurple);
        qRevenueBorderColors.push(isRefBar ? qRevenueRefBorder : qRevenueBarBorderDefault);
        qRevenueBorderWidths.push(isRefBar ? 2 : 1);
    }
    finaFixedAssetsChartInstance = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    type: 'bar',
                    label: '在建工程（亿）',
                    data: cipValues,
                    yAxisID: 'y',
                    order: 0,
                    backgroundColor: faPal.cipFill,
                    borderColor: faPal.cipBorder,
                    borderWidth: 1,
                    borderRadius: 4,
                    maxBarThickness: 11,
                    barPercentage: 0.72,
                },
                {
                    type: 'bar',
                    label: '固定资产（亿）',
                    data: fixAssets,
                    yAxisID: 'y',
                    order: 1,
                    backgroundColor: faPal.fixFill,
                    borderColor: faPal.fixBorder,
                    borderWidth: 1.2,
                    borderRadius: 4,
                    maxBarThickness: 11,
                    barPercentage: 0.72,
                },
                {
                    type: 'bar',
                    label: '存货（亿）',
                    data: inventories,
                    yAxisID: 'y',
                    order: 2,
                    backgroundColor: faPal.invFill,
                    borderColor: faPal.invBorder,
                    borderWidth: 1.2,
                    borderRadius: 4,
                    maxBarThickness: 11,
                    barPercentage: 0.72,
                },
                {
                    type: 'bar',
                    label: '营收（亿）',
                    data: qRevenue,
                    yAxisID: 'y',
                    order: 3,
                    backgroundColor: qRevenueBgColors,
                    borderColor: qRevenueBorderColors,
                    borderWidth: qRevenueBorderWidths,
                    borderRadius: 4,
                    maxBarThickness: 11,
                    barPercentage: 0.72,
                },
                {
                    type: 'line',
                    label: '合同负债（亿）',
                    data: contractLiab,
                    yAxisID: 'y1',
                    borderColor: faPal.lineMain,
                    backgroundColor: faPal.lineFillSoft,
                    borderWidth: 3,
                    fill: false,
                    tension: 0,
                    pointRadius: 4,
                    pointHoverRadius: 7,
                    pointBackgroundColor: 'rgba(255,255,255,1)',
                    pointBorderColor: faPal.lineMain,
                    pointBorderWidth: 2,
                    spanGaps: false,
                    order: 10,
                },
            ],
        },
        options: {
            animation: false,
            responsive: true,
            maintainAspectRatio: false,
            devicePixelRatio: getFinaFixedAssetsChartDpr(),
            layout: {
                padding: { top: 2, right: 8, bottom: 2, left: 2 },
            },
            interaction: { mode: 'index', intersect: false },
            datasets: {
                bar: {
                    categoryPercentage: 0.6,
                    barPercentage: 0.72,
                },
                line: {
                    borderWidth: 3,
                },
            },
            plugins: {
                legend: finaFixedAssetsLegendOptions(),
                tooltip: {
                    displayColors: false,
                    callbacks: {
                        title: function (items) {
                            return items.length ? String(items[0].label || '') : '';
                        },
                        label: function (ctx) {
                            var v = ctx.raw;
                            var n = typeof v === 'number' ? v : parseFloat(v);
                            var name = ctx.dataset.label || '';
                            if (isNaN(n)) return name + ': --';
                            return name + ': ' + n.toFixed(1);
                        },
                    },
                },
            },
            scales: {
                x: {
                    ticks: {
                        maxRotation: 45,
                        minRotation: 0,
                        autoSkip: true,
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                    },
                    grid: { display: false },
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    id: 'y',
                    title: { display: false },
                    ticks: {
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                        callback: function (val) {
                            return finaFixedAssetsAxisInt(val);
                        },
                    },
                    grid: {
                        color: 'rgba(226, 232, 240, 0.95)',
                        lineWidth: 0.5,
                    },
                },
                y1: {
                    type: 'linear',
                    position: 'right',
                    id: 'y1',
                    title: { display: false },
                    grid: {
                        drawOnChartArea: false,
                    },
                    ticks: {
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                        callback: function (val) {
                            return finaFixedAssetsAxisOneDecimal(val);
                        },
                    },
                },
            },
        },
        plugins: [
            {
                id: 'finaFixedAssetsLineGlow',
                /* 极淡底层描边，抬升折线可读性但不糊版 */
                beforeDatasetDraw: function (chart, args) {
                    var ds = chart.data.datasets[args.index];
                    if (!ds || ds.type !== 'line') return;
                    var meta = chart.getDatasetMeta(args.index);
                    if (!meta || meta.hidden || !meta.data || meta.data.length === 0) return;
                    var pts = meta.data;
                    var series = ds.data || [];
                    var ctx = chart.ctx;
                    ctx.save();
                    ctx.beginPath();
                    var started = false;
                    var pi = 0;
                    for (; pi < pts.length; pi += 1) {
                        var raw = series[pi];
                        if (raw == null || raw === '') {
                            started = false;
                            continue;
                        }
                        var p = pts[pi];
                        if (!p || p.skip) {
                            started = false;
                            continue;
                        }
                        if (!started) {
                            ctx.moveTo(p.x, p.y);
                            started = true;
                        } else {
                            ctx.lineTo(p.x, p.y);
                        }
                    }
                    ctx.strokeStyle = faPal.lineGlow;
                    ctx.lineWidth = 9;
                    ctx.lineJoin = 'round';
                    ctx.lineCap = 'round';
                    ctx.stroke();
                    ctx.restore();
                },
            },
            {
                id: 'finaFixedAssetsBarValueLabels',
                afterDatasetsDraw: function (chart) {
                    var ctx = chart.ctx;
                    var chartArea = chart.chartArea || {};
                    var placedLabelBoxes = [];
                    var occupiedByX = {};
                    /** 白描边 + 深色填字，柱线重叠区数字仍可读 */
                    function drawHaloLabel(txt, x, y, fillStyle) {
                        ctx.font = '600 ' + textPreset.labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'bottom';
                        ctx.lineJoin = 'round';
                        ctx.lineWidth = textPreset.labelStrokeWidth;
                        ctx.strokeStyle = 'rgba(255,255,255,0.96)';
                        ctx.fillStyle = fillStyle;
                        ctx.strokeText(txt, x, y);
                        ctx.fillText(txt, x, y);
                    }
                    function canPlaceLabelAt(txt, x, y) {
                        ctx.save();
                        ctx.font = '600 ' + textPreset.labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                        var textWidth = ctx.measureText(txt).width;
                        ctx.restore();
                        var padX = 4;
                        var padY = 2;
                        var labelHeight = textPreset.labelFontSize + 2;
                        var box = {
                            left: x - textWidth / 2 - padX,
                            right: x + textWidth / 2 + padX,
                            top: y - labelHeight - padY,
                            bottom: y + padY,
                        };
                        if (box.top < (chartArea.top || 0) + 2 || box.bottom > (chartArea.bottom || chart.height) - 2) {
                            return false;
                        }
                        var minGap = 2;
                        var i = 0;
                        for (; i < placedLabelBoxes.length; i += 1) {
                            var b = placedLabelBoxes[i];
                            var overlapX = !(box.right + minGap < b.left || box.left - minGap > b.right);
                            var overlapY = !(box.bottom + minGap < b.top || box.top - minGap > b.bottom);
                            if (overlapX && overlapY) return false;
                        }
                        placedLabelBoxes.push(box);
                        var xKey = String(Math.round(x));
                        if (!occupiedByX[xKey]) occupiedByX[xKey] = [];
                        occupiedByX[xKey].push(box);
                        return true;
                    }
                    function overlapsWithColumn(box, x) {
                        var xKey = String(Math.round(x));
                        var col = occupiedByX[xKey] || [];
                        var i = 0;
                        for (; i < col.length; i += 1) {
                            var b = col[i];
                            var overlapY = !(box.bottom + 2 < b.top || box.top - 2 > b.bottom);
                            if (overlapY) return true;
                        }
                        return false;
                    }
                    /**
                     * 先尝试多档上下避让，再决定是否隐藏。
                     * 这样既避免重叠，也尽量保留更多字段值。
                     */
                    function placeLabelWithAvoidance(txt, x, baseY) {
                        var offsets = [0, -11, 11, -22, 22, -33, 33, -44, 44, -55, 55, -66, 66];
                        var oi = 0;
                        for (; oi < offsets.length; oi += 1) {
                            var y = baseY + offsets[oi];
                            ctx.save();
                            ctx.font = '600 ' + textPreset.labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                            var textWidth = ctx.measureText(txt).width;
                            ctx.restore();
                            var candidate = {
                                left: x - textWidth / 2 - 4,
                                right: x + textWidth / 2 + 4,
                                top: y - (textPreset.labelFontSize + 4),
                                bottom: y + 2,
                            };
                            if (!overlapsWithColumn(candidate, x) && canPlaceLabelAt(txt, x, y)) return y;
                        }
                        // 极端拥挤时：顺轴向上堆叠，确保不漏标
                        var fallbackY = baseY;
                        var tryCount = 0;
                        while (tryCount < 14) {
                            if (canPlaceLabelAt(txt, x, fallbackY)) return fallbackY;
                            fallbackY -= 10;
                            tryCount += 1;
                        }
                        return Math.max((chartArea.top || 0) + textPreset.labelFontSize + 2, baseY - 80);
                    }
                    var datasetOrder = ['合同负债（亿）', '固定资产（亿）', '营收（亿）', '存货（亿）', '在建工程（亿）'];
                    datasetOrder.forEach(function (datasetName) {
                        var di = -1;
                        var i = 0;
                        for (; i < chart.data.datasets.length; i += 1) {
                            if ((chart.data.datasets[i] && chart.data.datasets[i].label) === datasetName) {
                                di = i;
                                break;
                            }
                        }
                        if (di < 0) return;
                        var dataset = chart.data.datasets[di];
                        var meta = chart.getDatasetMeta(di);
                        if (!meta || meta.hidden) return;
                        if (dataset.type === 'line') {
                            meta.data.forEach(function (pt, i) {
                                if (!pt || pt.skip) return;
                                var raw = dataset.data[i];
                                if (raw == null || raw === '') return;
                                var v = typeof raw === 'number' ? raw : parseFloat(raw);
                                if (isNaN(v)) return;
                                var x = pt.x;
                                var y = pt.y;
                                if (x == null || y == null) return;
                                var txt = v.toFixed(1);
                                var ty = placeLabelWithAvoidance(txt, x, y - 9);
                                if (ty == null) return;
                                ctx.save();
                                drawHaloLabel(
                                    txt,
                                    x,
                                    ty,
                                    faPal.lblLine
                                );
                                ctx.restore();
                            });
                            return;
                        }
                        meta.data.forEach(function (bar, i) {
                            var raw = dataset.data[i];
                            if (raw == null || raw === '') return;
                            var v = typeof raw === 'number' ? raw : parseFloat(raw);
                            if (isNaN(v)) return;
                            var x = bar.x;
                            var y = bar.y;
                            var base = bar.base;
                            if (x == null || y == null || base == null) return;
                            var top = Math.min(y, base);
                            ctx.save();
                            var refIdx = labels.length - 5;
                            var isRefRev =
                                dataset.label === '营收（亿）' && refIdx >= 0 && i === refIdx;
                            var fill =
                                dataset.label === '固定资产（亿）'
                                    ? faPal.lblFix
                                    : dataset.label === '存货（亿）'
                                      ? faPal.lblInv
                                    : dataset.label === '在建工程（亿）'
                                      ? faPal.lblCip
                                      : isRefRev
                                      ? faPal.lblRef
                                      : faPal.lblRev;
                            var txt = v.toFixed(1);
                            var baseTy = top - 2;
                            if (dataset.label === '固定资产（亿）') baseTy = top - 2;
                            else if (dataset.label === '营收（亿）') baseTy = top - 14;
                            else if (dataset.label === '存货（亿）') baseTy = top - 26;
                            else if (dataset.label === '在建工程（亿）') baseTy = top - 38;
                            var ty = placeLabelWithAvoidance(txt, x, baseTy);
                            if (ty == null) {
                                ctx.restore();
                                return;
                            }
                            drawHaloLabel(txt, x, ty, fill);
                            ctx.restore();
                        });
                    });
                },
            },
        ],
    });
    showFinaFixedAssetsMsg('');
    requestAnimationFrame(function () {
        if (finaFixedAssetsChartInstance && typeof finaFixedAssetsChartInstance.resize === 'function') {
            try {
                finaFixedAssetsChartInstance.resize();
            } catch (e) {
                /* ignore */
            }
        }
    });
}

function closeFinaCashCreateModal() {
    var overlay = document.getElementById('fina-cash-create-modal-overlay');
    if (!overlay) return;
    var wasOpen = overlay.classList.contains('is-open');
    var modal = overlay.querySelector('.fina-cash-create-modal-shell');
    if (wasOpen && modal) saveFinaCashCreateModalLayout(modal);
    overlay.classList.remove('is-open');
    showFinaCashCreateMsg('');
    if (finaCashCreateChartInstance) {
        finaCashCreateChartInstance.destroy();
        finaCashCreateChartInstance = null;
    }
    if (wasOpen) _modalClosed();
}

function renderFinaCashCreateChart(rows) {
    var canvas = document.getElementById('fina-cash-create-chart');
    if (!canvas) return;
    if (!window.Chart) {
        showFinaCashCreateMsg('图表组件未加载，请 Ctrl+F5 后重试。');
        return;
    }
    if (!Array.isArray(rows) || rows.length === 0) {
        showFinaCashCreateMsg('暂无可用现金创造能力数据');
        return;
    }
    var labels = [];
    var ocf = [];
    var capex = [];
    var fcf = [];
    var cashConv = [];
    rows.forEach(function (r) {
        r = r || {};
        labels.push(formatFinaQuarterLabel(r.end_date));
        var ocfV = parseFloat(r.q_ocf_yi);
        var capexV = parseFloat(r.q_capex_yi);
        var fcfV = parseFloat(r.q_fcf_star_yi);
        var convV = parseFloat(r.q_ocf_to_or_pct);
        ocf.push(isNaN(ocfV) ? null : ocfV);
        capex.push(isNaN(capexV) ? null : capexV);
        fcf.push(isNaN(fcfV) ? null : fcfV);
        cashConv.push(isNaN(convV) ? null : convV);
    });
    var textPreset = getFinaFixedAssetsChartTextPreset(labels.length);
    if (finaCashCreateChartInstance) {
        finaCashCreateChartInstance.destroy();
        finaCashCreateChartInstance = null;
    }
    var pal = {
        ocfFill: 'rgba(59, 130, 246, 0.78)',
        ocfBorder: 'rgba(37, 99, 235, 1)',
        /* 负值：靛青冷蓝，与正蓝同系、一眼区隔 */
        ocfNegFill: 'rgba(12, 74, 110, 0.78)',
        ocfNegBorder: 'rgba(6, 58, 88, 1)',
        capexFill: 'rgba(245, 158, 11, 0.78)',
        capexBorder: 'rgba(180, 83, 9, 1)',
        /* 负值：深土橙/褐，与正橙同系 */
        capexNegFill: 'rgba(154, 52, 18, 0.76)',
        capexNegBorder: 'rgba(92, 32, 12, 1)',
        fcfFill: 'rgba(139, 92, 246, 0.76)',
        fcfBorder: 'rgba(124, 58, 237, 1)',
        /* 与经营宽表 .fina-fa-opdata-negative 同系「绿示负」，与正 FCF 紫柱区分 */
        fcfNegFill: 'rgba(4, 120, 87, 0.78)',
        fcfNegBorder: 'rgba(6, 95, 70, 1)',
        line: 'rgba(219, 39, 119, 1)',
        lineSoft: 'rgba(219, 39, 119, 0.12)',
        /* 折线点负值边：与 fcf 负/宽表负值翡翠同系；正值仍用 line 玫红 */
        cashConvPointNeg: 'rgba(6, 95, 70, 1)',
    };
    function buildFinaCashBarPosNegColors(values, posFill, posBorder, negFill, negBorder) {
        var fills = [];
        var borders = [];
        values.forEach(function (v) {
            if (v == null || v === '' || (typeof v === 'number' && isNaN(v))) {
                fills.push(posFill);
                borders.push(posBorder);
                return;
            }
            var nv = typeof v === 'number' ? v : parseFloat(v);
            if (isNaN(nv)) {
                fills.push(posFill);
                borders.push(posBorder);
                return;
            }
            if (nv < 0) {
                fills.push(negFill);
                borders.push(negBorder);
            } else {
                fills.push(posFill);
                borders.push(posBorder);
            }
        });
        return { fills: fills, borders: borders };
    }
    var ocfB = buildFinaCashBarPosNegColors(
        ocf,
        pal.ocfFill,
        pal.ocfBorder,
        pal.ocfNegFill,
        pal.ocfNegBorder
    );
    var ocfBarFills = ocfB.fills;
    var ocfBarBorders = ocfB.borders;
    var capexB = buildFinaCashBarPosNegColors(
        capex,
        pal.capexFill,
        pal.capexBorder,
        pal.capexNegFill,
        pal.capexNegBorder
    );
    var capexBarFills = capexB.fills;
    var capexBarBorders = capexB.borders;
    var fcfB = buildFinaCashBarPosNegColors(
        fcf,
        pal.fcfFill,
        pal.fcfBorder,
        pal.fcfNegFill,
        pal.fcfNegBorder
    );
    var fcfBarFills = fcfB.fills;
    var fcfBarBorders = fcfB.borders;
    var cashConvPointBgs = [];
    var cashConvPointBorders = [];
    cashConv.forEach(function (v) {
        if (v == null || v === '' || (typeof v === 'number' && isNaN(v))) {
            cashConvPointBgs.push('rgba(255,255,255,1)');
            cashConvPointBorders.push(pal.line);
            return;
        }
        var nv = typeof v === 'number' ? v : parseFloat(v);
        if (isNaN(nv)) {
            cashConvPointBgs.push('rgba(255,255,255,1)');
            cashConvPointBorders.push(pal.line);
            return;
        }
        cashConvPointBgs.push('rgba(255,255,255,1)');
        cashConvPointBorders.push(nv < 0 ? pal.cashConvPointNeg : pal.line);
    });
    var labelFontSize = Math.max(10, Math.min(12, textPreset.labelFontSize - 1));
    var cashLabelPlugin = {
        id: 'finaCashCreateValueLabels',
        afterDatasetsDraw: function (chart) {
            var ctx = chart.ctx;
            var chartArea = chart.chartArea || {};
            var placed = [];
            function labelText(v, unit) {
                var n = typeof v === 'number' ? v : parseFloat(v);
                if (isNaN(n)) return '';
                return n.toFixed(1) + (unit || '');
            }
            function measureBox(txt, x, y) {
                ctx.save();
                ctx.font = '700 ' + labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                var w = ctx.measureText(txt).width;
                ctx.restore();
                var padX = 4;
                var padY = 2;
                var h = labelFontSize + 3;
                return {
                    left: x - w / 2 - padX,
                    right: x + w / 2 + padX,
                    top: y - h - padY,
                    bottom: y + padY,
                };
            }
            function fits(box) {
                var leftBound = (chartArea.left || 0) - 8;
                var rightBound = (chartArea.right || chart.width) + 8;
                var topBound = (chartArea.top || 0) - 2;
                var bottomBound = (chartArea.bottom || chart.height) + 2;
                if (box.left < leftBound || box.right > rightBound || box.top < topBound || box.bottom > bottomBound) {
                    return false;
                }
                for (var i = 0; i < placed.length; i += 1) {
                    var b = placed[i];
                    var overlapX = !(box.right + 2 < b.left || box.left - 2 > b.right);
                    var overlapY = !(box.bottom + 2 < b.top || box.top - 2 > b.bottom);
                    if (overlapX && overlapY) return false;
                }
                return true;
            }
            function drawLabel(txt, x, y, fillStyle) {
                ctx.save();
                ctx.font = '700 ' + labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'bottom';
                ctx.lineJoin = 'round';
                ctx.lineWidth = 3.5;
                ctx.strokeStyle = 'rgba(255,255,255,0.98)';
                ctx.fillStyle = fillStyle;
                ctx.strokeText(txt, x, y);
                ctx.fillText(txt, x, y);
                ctx.restore();
            }
            function placeLabel(txt, x, baseY, fillStyle, offsets) {
                for (var i = 0; i < offsets.length; i += 1) {
                    var y = baseY + offsets[i];
                    var box = measureBox(txt, x, y);
                    if (!fits(box)) continue;
                    placed.push(box);
                    drawLabel(txt, x, y, fillStyle);
                    return true;
                }
                return false;
            }
            var drawOrder = [
                '现金转化率',
                '自由现金流FCF（亿）',
                '经营现金流净额（亿）',
                '资本开支（亿）',
            ];
            drawOrder.forEach(function (name) {
                var di = -1;
                for (var i = 0; i < chart.data.datasets.length; i += 1) {
                    if ((chart.data.datasets[i] || {}).label === name) {
                        di = i;
                        break;
                    }
                }
                if (di < 0) return;
                var ds = chart.data.datasets[di];
                var meta = chart.getDatasetMeta(di);
                if (!meta || meta.hidden || !meta.data) return;
                meta.data.forEach(function (elem, idx) {
                    if (!elem || elem.skip) return;
                    var raw = ds.data[idx];
                    var n = typeof raw === 'number' ? raw : parseFloat(raw);
                    if (isNaN(n)) return;
                    if (ds.type === 'line') {
                        var lineLblC = n < 0 ? pal.cashConvPointNeg : pal.line;
                        placeLabel(labelText(n, ''), elem.x, elem.y - 8, lineLblC, [0, -12, 12, -24, 24, -36, 36]);
                        return;
                    }
                    var top = Math.min(elem.y, elem.base);
                    var bottom = Math.max(elem.y, elem.base);
                    var positive = n >= 0;
                    var fill =
                        ds.label === '经营现金流净额（亿）'
                            ? (positive ? pal.ocfBorder : pal.ocfNegBorder)
                            : ds.label === '资本开支（亿）'
                              ? (positive ? pal.capexBorder : pal.capexNegBorder)
                              : ds.label === '自由现金流FCF（亿）'
                                ? (positive ? pal.fcfBorder : pal.fcfNegBorder)
                                : pal.fcfBorder;
                    var baseY = positive ? top - 2 : bottom + labelFontSize + 4;
                    var offsets = positive
                        ? [0, -12, 12, -24, 24, -36, 36]
                        : [0, 12, -12, 24, -24, 36, -36];
                    placeLabel(labelText(n, ''), elem.x, baseY, fill, offsets);
                });
            });
        },
    };
    finaCashCreateChartInstance = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    type: 'bar',
                    label: '经营现金流净额（亿）',
                    data: ocf,
                    yAxisID: 'y',
                    backgroundColor: ocfBarFills,
                    borderColor: ocfBarBorders,
                    borderWidth: 1,
                    borderRadius: 4,
                    order: 1,
                },
                {
                    type: 'bar',
                    label: '资本开支（亿）',
                    data: capex,
                    yAxisID: 'y',
                    backgroundColor: capexBarFills,
                    borderColor: capexBarBorders,
                    borderWidth: 1,
                    borderRadius: 4,
                    order: 2,
                },
                {
                    type: 'bar',
                    label: '自由现金流FCF（亿）',
                    data: fcf,
                    yAxisID: 'y',
                    backgroundColor: fcfBarFills,
                    borderColor: fcfBarBorders,
                    borderWidth: 1,
                    borderRadius: 4,
                    order: 3,
                },
                {
                    type: 'line',
                    label: '现金转化率',
                    data: cashConv,
                    yAxisID: 'y1',
                    borderColor: pal.line,
                    backgroundColor: pal.lineSoft,
                    borderWidth: 3,
                    fill: false,
                    tension: 0,
                    pointRadius: 4,
                    pointHoverRadius: 7,
                    pointBackgroundColor: cashConvPointBgs,
                    pointBorderColor: cashConvPointBorders,
                    pointBorderWidth: 2,
                    spanGaps: false,
                    order: 10,
                },
            ],
        },
        options: {
            animation: false,
            responsive: true,
            maintainAspectRatio: false,
            devicePixelRatio: getFinaFixedAssetsChartDpr(),
            layout: { padding: { top: 18, right: 8, bottom: 6, left: 2 } },
            interaction: { mode: 'index', intersect: false },
            datasets: {
                bar: { categoryPercentage: 0.68, barPercentage: 0.86 },
                line: { borderWidth: 3 },
            },
            plugins: {
                legend: finaFixedAssetsLegendOptions(),
                tooltip: {
                    displayColors: false,
                    callbacks: {
                        title: function (items) {
                            return items.length ? String(items[0].label || '') : '';
                        },
                        label: function (ctx) {
                            var v = ctx.raw;
                            var n = typeof v === 'number' ? v : parseFloat(v);
                            var name = ctx.dataset.label || '';
                            if (isNaN(n)) return name + ': --';
                            var unit = ctx.dataset.yAxisID === 'y1' ? '' : '亿';
                            return name + ': ' + n.toFixed(1) + unit;
                        },
                    },
                },
            },
            scales: {
                x: {
                    ticks: {
                        maxRotation: 45,
                        minRotation: 0,
                        autoSkip: true,
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                    },
                    grid: { display: false },
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    title: { display: false },
                    ticks: {
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                        callback: function (val) {
                            return finaFixedAssetsAxisInt(val);
                        },
                    },
                    grid: {
                        color: function (ctx) {
                            return Number(ctx.tick && ctx.tick.value) === 0 ? '#90a4ae' : 'rgba(226, 232, 240, 0.95)';
                        },
                        lineWidth: function (ctx) {
                            return Number(ctx.tick && ctx.tick.value) === 0 ? 2.25 : 0.5;
                        },
                    },
                },
                y1: {
                    type: 'linear',
                    position: 'right',
                    title: { display: false },
                    grid: { drawOnChartArea: false },
                    ticks: {
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                        callback: function (val) {
                            return finaFixedAssetsAxisOneDecimal(val);
                        },
                    },
                },
            },
        },
        plugins: [cashLabelPlugin],
    });
    showFinaCashCreateMsg('');
    requestAnimationFrame(function () {
        if (finaCashCreateChartInstance && typeof finaCashCreateChartInstance.resize === 'function') {
            try {
                finaCashCreateChartInstance.resize();
            } catch (e) {
                /* ignore */
            }
        }
    });
}

function openFinaCashCreateModal() {
    var overlay = document.getElementById('fina-cash-create-modal-overlay');
    var titleEl = document.getElementById('fina-cash-create-modal-title');
    if (!overlay) return;
    finaCashCreateModalLayoutOpenTs = Date.now();
    var rows = Array.isArray(finaFixedAssetsCurrentRows) ? finaFixedAssetsCurrentRows : [];
    if (titleEl) {
        titleEl.textContent = finaFixedAssetsCurrentTitle
            ? finaFixedAssetsCurrentTitle + ' · 现金创造能力'
            : '现金创造能力';
    }
    overlay.classList.add('is-open');
    _modalOpened();
    applyFinaCashCreateModalSavedLayout();
    if (!rows.length) {
        showFinaCashCreateMsg('当前弹窗尚未加载到 25 季经营数据');
        return;
    }
    renderFinaCashCreateChart(rows);
}

function closeFinaWorkingCapModal() {
    var overlay = document.getElementById('fina-working-cap-modal-overlay');
    if (!overlay) return;
    var wasOpen = overlay.classList.contains('is-open');
    var modal = overlay.querySelector('.fina-working-cap-modal-shell');
    if (wasOpen && modal) saveFinaWorkingCapModalLayout(modal);
    overlay.classList.remove('is-open');
    showFinaWorkingCapMsg('');
    if (finaWorkingCapTurnoverChartInstance) {
        finaWorkingCapTurnoverChartInstance.destroy();
        finaWorkingCapTurnoverChartInstance = null;
    }
    if (wasOpen) _modalClosed();
}

function renderFinaWorkingCapTurnoverChart(rows) {
    var canvas = document.getElementById('fina-working-cap-chart');
    if (!canvas) return;
    if (!window.Chart) {
        showFinaWorkingCapMsg('图表组件未加载，请 Ctrl+F5 后重试。');
        return;
    }
    if (!Array.isArray(rows) || rows.length === 0) {
        showFinaWorkingCapMsg('暂无可用营运资本周转数据');
        return;
    }
    var labels = [];
    var arDays = [];
    var invDays = [];
    var apDays = [];
    var cccDays = [];
    var recvPct = [];
    rows.forEach(function (r) {
        r = r || {};
        labels.push(formatFinaQuarterLabel(r.end_date));
        var arV = parseFloat(r.ar_days_q);
        var invV = parseFloat(r.inv_days_q);
        var apV = parseFloat(r.ap_days_q);
        var cccV = parseFloat(r.ccc_q);
        var recvV = parseFloat(r.recv_pct);
        arDays.push(isNaN(arV) ? null : arV);
        invDays.push(isNaN(invV) ? null : invV);
        apDays.push(isNaN(apV) ? null : apV);
        cccDays.push(isNaN(cccV) ? null : cccV);
        recvPct.push(isNaN(recvV) ? null : recvV);
    });
    var textPreset = getFinaFixedAssetsChartTextPreset(labels.length);
    var labelFontSize = Math.max(10, Math.min(12, textPreset.labelFontSize - 1));
    if (finaWorkingCapTurnoverChartInstance) finaWorkingCapTurnoverChartInstance.destroy();
    var pal = {
        arLine: 'rgba(37, 99, 235, 1)',
        invLine: 'rgba(234, 88, 12, 1)',
        apLine: 'rgba(5, 150, 105, 1)',
        cccLine: 'rgba(219, 39, 119, 1)',
        recvBarFill: 'rgba(14, 165, 233, 0.28)',
        recvBarBorder: 'rgba(2, 132, 199, 0.96)',
    };
    var wcValueLabelPlugin = {
        id: 'finaWorkingCapValueLabels',
        afterDatasetsDraw: function (chart) {
            var ctx = chart.ctx;
            var chartArea = chart.chartArea || {};
            var placed = [];
            function labelText(v) {
                var n = typeof v === 'number' ? v : parseFloat(v);
                if (isNaN(n)) return '';
                return n.toFixed(1);
            }
            function measureBox(txt, x, y) {
                ctx.save();
                ctx.font = '700 ' + labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                var w = ctx.measureText(txt).width;
                ctx.restore();
                var padX = 4;
                var padY = 2;
                var h = labelFontSize + 3;
                return {
                    left: x - w / 2 - padX,
                    right: x + w / 2 + padX,
                    top: y - h - padY,
                    bottom: y + padY,
                };
            }
            function fits(box) {
                var leftBound = (chartArea.left || 0) - 8;
                var rightBound = (chartArea.right || chart.width) + 8;
                var topBound = (chartArea.top || 0) - 2;
                var bottomBound = (chartArea.bottom || chart.height) + 2;
                if (box.left < leftBound || box.right > rightBound || box.top < topBound || box.bottom > bottomBound) {
                    return false;
                }
                for (var i = 0; i < placed.length; i += 1) {
                    var b = placed[i];
                    var overlapX = !(box.right + 2 < b.left || box.left - 2 > b.right);
                    var overlapY = !(box.bottom + 2 < b.top || box.top - 2 > b.bottom);
                    if (overlapX && overlapY) return false;
                }
                return true;
            }
            function drawLabel(txt, x, y, fillStyle) {
                ctx.save();
                ctx.font = '700 ' + labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'bottom';
                ctx.lineJoin = 'round';
                ctx.lineWidth = 3.6;
                ctx.strokeStyle = 'rgba(255,255,255,0.98)';
                ctx.fillStyle = fillStyle;
                ctx.strokeText(txt, x, y);
                ctx.fillText(txt, x, y);
                ctx.restore();
            }
            function placeLabel(txt, x, baseY, fillStyle, offsets) {
                for (var i = 0; i < offsets.length; i += 1) {
                    var y = baseY + offsets[i];
                    var box = measureBox(txt, x, y);
                    if (!fits(box)) continue;
                    placed.push(box);
                    drawLabel(txt, x, y, fillStyle);
                    return true;
                }
                return false;
            }
            var drawOrder = ['现金循环天数（天）', '应付天数（天）', '应收天数（天）', '存货天数（天）', '应收占收入比'];
            drawOrder.forEach(function (name) {
                var di = -1;
                for (var i = 0; i < chart.data.datasets.length; i += 1) {
                    if ((chart.data.datasets[i] || {}).label === name) {
                        di = i;
                        break;
                    }
                }
                if (di < 0) return;
                var ds = chart.data.datasets[di];
                var meta = chart.getDatasetMeta(di);
                if (!meta || meta.hidden || !meta.data) return;
                meta.data.forEach(function (elem, idx) {
                    if (!elem || elem.skip) return;
                    var raw = ds.data[idx];
                    var n = typeof raw === 'number' ? raw : parseFloat(raw);
                    if (isNaN(n)) return;
                    var txt = labelText(n);
                    if (!txt) return;
                    if (ds.type === 'bar') {
                        var top = Math.min(elem.y, elem.base);
                        placeLabel(txt, elem.x, top - 2, pal.recvBarBorder, [0, -10, 10, -20, 20]);
                        return;
                    }
                    var c = ds.label === '应收天数（天）'
                        ? pal.arLine
                        : ds.label === '存货天数（天）'
                          ? pal.invLine
                          : ds.label === '应付天数（天）'
                            ? pal.apLine
                            : pal.cccLine;
                    placeLabel(txt, elem.x, elem.y - 8, c, [0, -12, 12, -24, 24, -36, 36]);
                });
            });
        },
    };
    finaWorkingCapTurnoverChartInstance = new Chart(canvas, {
        data: {
            labels: labels,
            datasets: [
                {
                    type: 'line',
                    label: '应收天数（天）',
                    data: arDays,
                    yAxisID: 'y',
                    borderColor: pal.arLine,
                    backgroundColor: 'rgba(37, 99, 235, 0.1)',
                    pointBackgroundColor: '#fff',
                    pointBorderColor: pal.arLine,
                    pointRadius: 3.6,
                    pointHoverRadius: 6.6,
                    pointStyle: 'circle',
                    borderWidth: 2.6,
                    tension: 0,
                    order: 1,
                },
                {
                    type: 'line',
                    label: '存货天数（天）',
                    data: invDays,
                    yAxisID: 'y',
                    borderColor: pal.invLine,
                    backgroundColor: 'rgba(234, 88, 12, 0.1)',
                    pointBackgroundColor: '#fff',
                    pointBorderColor: pal.invLine,
                    pointRadius: 3.6,
                    pointHoverRadius: 6.6,
                    pointStyle: 'rectRounded',
                    borderWidth: 2.5,
                    tension: 0,
                    order: 2,
                },
                {
                    type: 'line',
                    label: '应付天数（天）',
                    data: apDays,
                    yAxisID: 'y',
                    borderColor: pal.apLine,
                    backgroundColor: 'rgba(5, 150, 105, 0.12)',
                    pointBackgroundColor: '#fff',
                    pointBorderColor: pal.apLine,
                    pointRadius: 3.8,
                    pointHoverRadius: 6.8,
                    pointStyle: 'triangle',
                    borderWidth: 2.8,
                    tension: 0,
                    order: 3,
                },
                {
                    type: 'line',
                    label: '现金循环天数（天）',
                    data: cccDays,
                    yAxisID: 'y',
                    borderColor: pal.cccLine,
                    backgroundColor: 'rgba(219, 39, 119, 0.1)',
                    pointBackgroundColor: '#fff',
                    pointBorderColor: pal.cccLine,
                    pointRadius: 4,
                    pointHoverRadius: 7,
                    pointStyle: 'star',
                    borderWidth: 2.8,
                    tension: 0,
                    order: 4,
                },
                {
                    type: 'bar',
                    label: '应收占收入比',
                    data: recvPct,
                    yAxisID: 'y1',
                    backgroundColor: pal.recvBarFill,
                    borderColor: pal.recvBarBorder,
                    borderWidth: 0.8,
                    borderRadius: 3,
                    barPercentage: 0.48,
                    categoryPercentage: 0.56,
                    maxBarThickness: 12,
                    order: 10,
                },
            ],
        },
        options: {
            animation: false,
            responsive: true,
            maintainAspectRatio: false,
            devicePixelRatio: getFinaFixedAssetsChartDpr(),
            layout: { padding: { top: 18, right: 8, bottom: 6, left: 2 } },
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: finaFixedAssetsLegendOptions(),
                tooltip: {
                    displayColors: false,
                    callbacks: {
                        title: function (items) {
                            return items.length ? String(items[0].label || '') : '';
                        },
                        label: function (ctx) {
                            var n = parseFloat(ctx.raw);
                            var name = ctx.dataset.label || '';
                            if (isNaN(n)) return name + ': --';
                            var unit = ctx.dataset.yAxisID === 'y1' ? '' : '天';
                            return name + ': ' + n.toFixed(1) + unit;
                        },
                    },
                },
            },
            scales: {
                x: {
                    ticks: {
                        maxRotation: 45,
                        minRotation: 0,
                        autoSkip: true,
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                    },
                    grid: { display: false },
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    ticks: {
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                        callback: function (v) {
                            return finaFixedAssetsAxisOneDecimal(v);
                        },
                    },
                    grid: {
                        color: function (c) {
                            return Number(c.tick && c.tick.value) === 0 ? '#90a4ae' : 'rgba(226,232,240,0.95)';
                        },
                        lineWidth: function (c) {
                            return Number(c.tick && c.tick.value) === 0 ? 2 : 0.5;
                        },
                    },
                },
                y1: {
                    type: 'linear',
                    position: 'right',
                    grid: { drawOnChartArea: false },
                    grace: '12%',
                    ticks: {
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                        callback: function (v) {
                            return finaFixedAssetsAxisOneDecimal(v);
                        },
                    },
                },
            },
        },
        plugins: [wcValueLabelPlugin],
    });
    showFinaWorkingCapMsg('');
    requestAnimationFrame(function(){try{if(finaWorkingCapTurnoverChartInstance&&finaWorkingCapTurnoverChartInstance.resize)finaWorkingCapTurnoverChartInstance.resize();}catch(e){}});
}

function openFinaWorkingCapModal() {
    var overlay = document.getElementById('fina-working-cap-modal-overlay');
    var titleEl = document.getElementById('fina-working-cap-modal-title');
    if (!overlay) return;
    finaWorkingCapModalLayoutOpenTs = Date.now();
    var rows = Array.isArray(finaFixedAssetsCurrentRows) ? finaFixedAssetsCurrentRows : [];
    if (titleEl) titleEl.textContent = finaFixedAssetsCurrentTitle ? finaFixedAssetsCurrentTitle + ' · 营运资本周转天数' : '营运资本周转天数';
    overlay.classList.add('is-open');
    _modalOpened();
    applyFinaWorkingCapModalSavedLayout();
    if (!rows.length) {
        showFinaWorkingCapMsg('当前弹窗尚未加载到 25 季经营数据');
        return;
    }
    renderFinaWorkingCapTurnoverChart(rows);
}

function closeFinaCapEffModal() {
    var overlay = document.getElementById('fina-cap-eff-modal-overlay');
    if (!overlay) return;
    var wasOpen = overlay.classList.contains('is-open');
    var modal = overlay.querySelector('.fina-cap-eff-modal-shell');
    if (wasOpen && modal) saveFinaCapEffModalLayout(modal);
    overlay.classList.remove('is-open');
    showFinaCapEffMsg('');
    if (finaCapEffChartInstance) {
        finaCapEffChartInstance.destroy();
        finaCapEffChartInstance = null;
    }
    if (wasOpen) _modalClosed();
}

function renderFinaCapEffChart(rows) {
    var canvas = document.getElementById('fina-cap-eff-chart');
    if (!canvas) return;
    if (!window.Chart) {
        showFinaCapEffMsg('图表组件未加载，请 Ctrl+F5 后重试。');
        return;
    }
    if (!Array.isArray(rows) || rows.length === 0) {
        showFinaCapEffMsg('暂无可用资本效率数据');
        return;
    }
    var labels = [];
    var roicArr = [];
    var roeArr = [];
    rows.forEach(function (r) {
        r = r || {};
        labels.push(formatFinaQuarterLabel(r.end_date));
        var roicV = parseFloat(r.roic_sq_pct_avg_ic_q);
        var roeV = parseFloat(r.q_dt_roe);
        roicArr.push(isNaN(roicV) ? null : roicV);
        roeArr.push(isNaN(roeV) ? null : roeV);
    });
    if (finaCapEffChartInstance) finaCapEffChartInstance.destroy();
    var textPreset = getFinaFixedAssetsChartTextPreset(labels.length);
    var pal = {
        roicLine: 'rgba(30, 136, 229, 1)',
        roicSoft: 'rgba(30, 136, 229, 0.14)',
        roicNeg: 'rgba(230, 81, 0, 1)',
        roicNegSoft: 'rgba(230, 81, 0, 0.14)',
        roeLine: 'rgba(171, 71, 188, 1)',
        roeSoft: 'rgba(171, 71, 188, 0.14)',
        roeNeg: 'rgba(198, 40, 40, 1)',
        roeNegSoft: 'rgba(198, 40, 40, 0.14)',
    };
    var capEffLabelPlugin = {
        id: 'finaCapEffValueLabels',
        afterDatasetsDraw: function (chart) {
            var ctx = chart.ctx;
            var chartArea = chart.chartArea || {};
            var labelFontSize = Math.max(10, Math.min(12, textPreset.labelFontSize - 1));
            var placed = [];
            var slotByIndex = {};
            function drawLabel(txt, x, y, color) {
                ctx.save();
                ctx.font = '700 ' + labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'bottom';
                ctx.lineJoin = 'round';
                ctx.lineWidth = 3.4;
                ctx.strokeStyle = 'rgba(255,255,255,0.98)';
                ctx.fillStyle = color;
                ctx.strokeText(txt, x, y);
                ctx.fillText(txt, x, y);
                ctx.restore();
            }
            function hasOverlap(box) {
                if (
                    box.left < (chartArea.left || 0) - 8 ||
                    box.right > (chartArea.right || chart.width) + 8 ||
                    box.top < (chartArea.top || 0) - 2 ||
                    box.bottom > (chartArea.bottom || chart.height) + 2
                ) {
                    return true;
                }
                for (var i = 0; i < placed.length; i += 1) {
                    var b = placed[i];
                    var overlapX = !(box.right + 2 < b.left || box.left - 2 > b.right);
                    var overlapY = !(box.bottom + 2 < b.top || box.top - 2 > b.bottom);
                    if (overlapX && overlapY) return true;
                }
                return false;
            }
            chart.data.datasets.forEach(function (ds, di) {
                var meta = chart.getDatasetMeta(di);
                if (!meta || meta.hidden || !meta.data) return;
                var isRoic = ds.label === 'ROIC';
                meta.data.forEach(function (elem, idx) {
                    if (!elem || elem.skip) return;
                    var n = parseFloat(ds.data[idx]);
                    if (isNaN(n)) return;
                    var txt = n.toFixed(2);
                    var x = elem.x;
                    var yBase = elem.y - 8;
                    var color = isRoic
                        ? (n < 0 ? pal.roicNeg : pal.roicLine)
                        : (n < 0 ? pal.roeNeg : pal.roeLine);
                    var slot = slotByIndex[idx] || 0;
                    slotByIndex[idx] = slot + 1;
                    var isEdgePoint = idx === 0 || idx === ds.data.length - 1;
                    var preferredOffsets = slot % 2 === 0
                        ? [-10, -22, -34, 12, 24, 36]
                        : [14, 26, 38, -8, -20, -32];
                    var offsets = isEdgePoint
                        ? preferredOffsets.concat([48, -44, 56, -52])
                        : preferredOffsets;
                    var xJitter = isEdgePoint
                        ? (idx === ds.data.length - 1
                            ? (slot % 2 === 0 ? -14 : 14)
                            : (slot % 2 === 0 ? -8 : 8))
                        : 0;
                    var oi;
                    var placedOk = false;
                    for (oi = 0; oi < offsets.length; oi += 1) {
                        var y = yBase + offsets[oi];
                        var drawX = x + xJitter;
                        ctx.save();
                        ctx.font = '700 ' + labelFontSize + 'px ' + FINA_CHART_PAGE_FONT;
                        var w = ctx.measureText(txt).width;
                        ctx.restore();
                        var box = {
                            left: drawX - w / 2 - 4,
                            right: drawX + w / 2 + 4,
                            top: y - (labelFontSize + 5),
                            bottom: y + 2,
                        };
                        if (hasOverlap(box)) continue;
                        placed.push(box);
                        drawLabel(txt, drawX, y, color);
                        placedOk = true;
                        break;
                    }
                    if (!placedOk && isEdgePoint) {
                        var fallbackY = yBase + (slot % 2 === 0 ? -44 : 44);
                        drawLabel(txt, x + xJitter, fallbackY, color);
                    }
                });
            });
        },
    };
    finaCapEffChartInstance = new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    type: 'line',
                    label: 'ROIC',
                    data: roicArr,
                    yAxisID: 'y',
                    borderColor: pal.roicLine,
                    backgroundColor: pal.roicSoft,
                    segment: {
                        borderColor: function (ctx) {
                            var y0 = ctx.p0 && ctx.p0.parsed ? ctx.p0.parsed.y : NaN;
                            var y1 = ctx.p1 && ctx.p1.parsed ? ctx.p1.parsed.y : NaN;
                            return (y0 < 0 || y1 < 0) ? pal.roicNeg : pal.roicLine;
                        },
                        backgroundColor: function (ctx) {
                            var y0 = ctx.p0 && ctx.p0.parsed ? ctx.p0.parsed.y : NaN;
                            var y1 = ctx.p1 && ctx.p1.parsed ? ctx.p1.parsed.y : NaN;
                            return (y0 < 0 || y1 < 0) ? pal.roicNegSoft : pal.roicSoft;
                        },
                    },
                    pointBackgroundColor: function (ctx) {
                        var v = parseFloat(ctx.raw);
                        return !isNaN(v) && v < 0 ? '#ffe9dc' : '#fff';
                    },
                    pointBorderColor: function (ctx) {
                        var v = parseFloat(ctx.raw);
                        return !isNaN(v) && v < 0 ? pal.roicNeg : pal.roicLine;
                    },
                    pointRadius: 3.8,
                    pointHoverRadius: 6.8,
                    pointStyle: 'circle',
                    borderWidth: 2.8,
                    tension: 0,
                    order: 1,
                },
                {
                    type: 'line',
                    label: 'ROE(扣非)',
                    data: roeArr,
                    yAxisID: 'y',
                    borderColor: pal.roeLine,
                    backgroundColor: pal.roeSoft,
                    segment: {
                        borderColor: function (ctx) {
                            var y0 = ctx.p0 && ctx.p0.parsed ? ctx.p0.parsed.y : NaN;
                            var y1 = ctx.p1 && ctx.p1.parsed ? ctx.p1.parsed.y : NaN;
                            return (y0 < 0 || y1 < 0) ? pal.roeNeg : pal.roeLine;
                        },
                        backgroundColor: function (ctx) {
                            var y0 = ctx.p0 && ctx.p0.parsed ? ctx.p0.parsed.y : NaN;
                            var y1 = ctx.p1 && ctx.p1.parsed ? ctx.p1.parsed.y : NaN;
                            return (y0 < 0 || y1 < 0) ? pal.roeNegSoft : pal.roeSoft;
                        },
                    },
                    pointBackgroundColor: function (ctx) {
                        var v = parseFloat(ctx.raw);
                        return !isNaN(v) && v < 0 ? '#fde7e7' : '#fff';
                    },
                    pointBorderColor: function (ctx) {
                        var v = parseFloat(ctx.raw);
                        return !isNaN(v) && v < 0 ? pal.roeNeg : pal.roeLine;
                    },
                    pointRadius: 3.8,
                    pointHoverRadius: 6.8,
                    pointStyle: 'rectRounded',
                    borderWidth: 2.8,
                    tension: 0,
                    order: 2,
                },
            ],
        },
        options: {
            animation: false,
            responsive: true,
            maintainAspectRatio: false,
            devicePixelRatio: getFinaFixedAssetsChartDpr(),
            layout: { padding: { top: 18, right: 8, bottom: 6, left: 2 } },
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: finaFixedAssetsLegendOptions(),
                tooltip: {
                    displayColors: false,
                    callbacks: {
                        title: function (items) {
                            return items.length ? String(items[0].label || '') : '';
                        },
                        label: function (ctx) {
                            var n = parseFloat(ctx.raw);
                            var name = ctx.dataset.label || '';
                            if (isNaN(n)) return name + ': --';
                            return name + ': ' + n.toFixed(2);
                        },
                    },
                },
            },
            scales: {
                x: {
                    ticks: {
                        maxRotation: 45,
                        minRotation: 0,
                        autoSkip: true,
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                    },
                    grid: { display: false },
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    ticks: {
                        color: '#64748b',
                        font: { size: textPreset.axisFontSize, family: FINA_CHART_PAGE_FONT },
                        callback: function (v) {
                            return finaFixedAssetsAxisOneDecimal(v);
                        },
                    },
                    grid: {
                        color: function (c) {
                            return Number(c.tick && c.tick.value) === 0 ? '#90a4ae' : 'rgba(226,232,240,0.95)';
                        },
                        lineWidth: function (c) {
                            return Number(c.tick && c.tick.value) === 0 ? 2 : 0.5;
                        },
                    },
                },
            },
        },
        plugins: [capEffLabelPlugin],
    });
    showFinaCapEffMsg('');
    requestAnimationFrame(function () {
        try {
            if (finaCapEffChartInstance && finaCapEffChartInstance.resize) finaCapEffChartInstance.resize();
        } catch (e) {
            /* ignore */
        }
    });
}

function openFinaCapEffModal() {
    var overlay = document.getElementById('fina-cap-eff-modal-overlay');
    var titleEl = document.getElementById('fina-cap-eff-modal-title');
    if (!overlay) return;
    finaCapEffModalLayoutOpenTs = Date.now();
    var rows = Array.isArray(finaFixedAssetsCurrentRows) ? finaFixedAssetsCurrentRows : [];
    if (titleEl) titleEl.textContent = finaFixedAssetsCurrentTitle ? finaFixedAssetsCurrentTitle + ' · 资本效率' : '资本效率';
    overlay.classList.add('is-open');
    _modalOpened();
    applyFinaCapEffModalSavedLayout();
    if (!rows.length) {
        showFinaCapEffMsg('当前弹窗尚未加载到 25 季经营数据');
        return;
    }
    renderFinaCapEffChart(rows);
}

/** 全角数字转 ASCII，避免输入法全角下 6 位无法匹配 */
function finaTsCodeAsciiDigits(s) {
    return String(s || '').replace(/[\uFF10-\uFF19]/g, function (ch) {
        return String.fromCharCode(ch.charCodeAt(0) - 0xff10 + 0x30);
    });
}

/** 按 6 位数字推断 .SH/.SZ/.BJ（与 stock_wind.rt_k_code_category / rt_k_代码分类.py A 股号段一致） */
function inferTsCodeSuffixFromSixDigits(d6) {
    if (!/^\d{6}$/.test(d6)) return '';
    /* 沪主板 600–609 段（含 602/606/607 等）；688/689 科创板单独 */
    if (/^60[0-9]\d{3}$/.test(d6)) return d6 + '.SH';
    if (/^(688|689)\d{3}$/.test(d6)) return d6 + '.SH';
    if (/^(000|001)\d{3}$/.test(d6)) return d6 + '.SZ';
    if (/^(002|003)\d{3}$/.test(d6)) return d6 + '.SZ';
    if (/^(300|301|302)\d{3}$/.test(d6)) return d6 + '.SZ';
    if (/^[48]\d{5}$/.test(d6)) return d6 + '.BJ';
    if (/^920\d{3}$/.test(d6)) return d6 + '.BJ';
    return '';
}

/** 弹窗用：Tushare 股票代码 6 位.交易所；6 位可自动补后缀；带后缀时若号段可识别则纠正错误后缀（如 600000.SZ→600000.SH） */
function normalizeFinaFixedAssetsTsCode(raw) {
    var s = finaTsCodeAsciiDigits(String(raw || ''))
        .trim()
        .replace(/\s/g, '')
        .toUpperCase();
    if (!s) return '';
    var digits = '';
    var suff = '';
    if (/^\d{6}\.(SH|SZ|BJ)$/.test(s)) {
        digits = s.slice(0, 6);
        suff = s.slice(7);
    } else {
        var m = /^(\d{6})(\.?)(SH|SZ|BJ)$/.exec(s);
        if (m) {
            digits = m[1];
            suff = m[3];
        }
    }
    if (digits && suff) {
        var inferred = inferTsCodeSuffixFromSixDigits(digits);
        if (inferred) return inferred;
        return digits + '.' + suff;
    }
    if (/^\d{6}$/.test(s)) {
        var inf = inferTsCodeSuffixFromSixDigits(s);
        return inf || '';
    }
    return '';
}

/** 固定资产弹窗输入框：仅展示 6 位，不显示 .SH/.SZ/.BJ（与 normalize 后完整 ts_code 配合使用） */
function formatFinaFixedAssetsTsCodeDisplay(tsCodeAny) {
    var s = finaTsCodeAsciiDigits(String(tsCodeAny || ''))
        .trim()
        .replace(/\s/g, '')
        .toUpperCase();
    if (!s) return '';
    var mDot = /^(\d{6})(\.(SH|SZ|BJ))$/.exec(s);
    if (mDot) return mDot[1];
    var mJoin = /^(\d{6})(SH|SZ|BJ)$/.exec(s);
    if (mJoin) return mJoin[1];
    if (/^\d{6}$/.test(s)) return s;
    return s;
}

/** 表头单行：仅输入框显示代码；名称与主题文案分列，整体居中（不重复代码） */
function updateFinaFixedAssetsHeaderLine(_tsCode, secName) {
    var secEl = document.getElementById('fina-fixed-assets-modal-sec-name');
    var topicEl = document.getElementById('fina-fixed-assets-modal-topic');
    var centerEl = document.querySelector('.fina-fixed-assets-header-center');
    var topic = FINA_FIXED_ASSETS_HEADER_CHART_TITLE;
    if (topicEl) topicEl.textContent = topic;
    if (secEl) {
        var name = String(secName || '').trim();
        secEl.textContent = name;
    }
    if (centerEl) {
        var parts = [];
        if (secEl && secEl.textContent) parts.push(secEl.textContent);
        parts.push(topic);
        centerEl.title = parts.length ? parts.join(' — ') : topic;
    }
}

/** 已在弹窗内时切换股票：不重复打开遮罩、不重复 _modalOpened */
function switchFinaFixedAssetsModalRow(tsCode, secName) {
    var raw = String(tsCode || '').trim();
    var normalized = normalizeFinaFixedAssetsTsCode(raw);
    var inp = document.getElementById('fina-fixed-assets-ts-input');
    if (inp) inp.value = formatFinaFixedAssetsTsCodeDisplay(normalized || raw);
    updateFinaFixedAssetsHeaderLine(normalized || raw, secName || '');
    fetchFinaFixedAssetsSeries(inp ? inp.value : raw);
}

/** 按当前表顺序（fixedAssetsNavList）在列表中前/后移动一条并加载 */
function navigateFinaFixedAssetsModalInList(delta) {
    var list = _finaMetricState().fixedAssetsNavList || [];
    var inp = document.getElementById('fina-fixed-assets-ts-input');
    var cur = inp ? normalizeFinaFixedAssetsTsCode(inp.value) : '';
    var idx = -1;
    var k = 0;
    for (; k < list.length; k++) {
        if (normalizeFinaFixedAssetsTsCode(list[k].ts_code) === cur) {
            idx = k;
            break;
        }
    }
    if (idx < 0) return;
    var ni = idx + delta;
    if (ni < 0 || ni >= list.length) return;
    var item = list[ni];
    switchFinaFixedAssetsModalRow(item.ts_code, item.sec_name);
}

function syncFinaFixedAssetsNavButtons() {
    var prev = document.getElementById('fina-fixed-assets-nav-prev');
    var next = document.getElementById('fina-fixed-assets-nav-next');
    if (!prev || !next) return;
    var list = _finaMetricState().fixedAssetsNavList || [];
    var inp = document.getElementById('fina-fixed-assets-ts-input');
    var cur = inp ? normalizeFinaFixedAssetsTsCode(inp.value) : '';
    var idx = -1;
    var j = 0;
    for (; j < list.length; j++) {
        if (normalizeFinaFixedAssetsTsCode(list[j].ts_code) === cur) {
            idx = j;
            break;
        }
    }
    var canNav = list.length > 0 && idx >= 0;
    prev.disabled = !canNav || idx <= 0;
    next.disabled = !canNav || idx >= list.length - 1;
}

function fetchFinaFixedAssetsSeries(tsCodeRaw) {
    var overlay = document.getElementById('fina-fixed-assets-modal-overlay');
    if (!overlay) return;
    var normalized = normalizeFinaFixedAssetsTsCode(tsCodeRaw);
    if (!normalized) {
        showFinaFixedAssetsMsg(
            '请输入 6 位股票代码（可自动补 .SH/.SZ/.BJ）或完整代码，如 603223、603223.SH、000001.SZ'
        );
        syncFinaFixedAssetsNavButtons();
        return;
    }
    var inp = document.getElementById('fina-fixed-assets-ts-input');
    if (inp) inp.value = formatFinaFixedAssetsTsCodeDisplay(normalized);
    var selectedEndDate = String(_finaMetricState().selectedEndDate || '').trim();
    if (!selectedEndDate) {
        showFinaFixedAssetsMsg('当前报告期为空，无法加载图表');
        syncFinaFixedAssetsNavButtons();
        return;
    }
    showFinaFixedAssetsMsg('加载中...');
    clearFinaFixedAssetsMetricsBar();
    clearFinaFixedAssetsMainbzTable();
    clearFinaFixedAssetsOperatingDataTable();
    finaFixedAssetsCurrentRows = [];
    finaFixedAssetsCurrentTitle = '';
    var cb = encodeURIComponent(String(Date.now()));
    var url = rtMonitorApiUrl(
        '/api/fina_fixed_assets_revenue_series?ts_code=' +
            encodeURIComponent(normalized) +
            '&end_date=' +
            encodeURIComponent(selectedEndDate) +
            '&limit=' +
            encodeURIComponent(String(FINA_FIXED_ASSETS_SERIES_LIMIT)) +
            '&_cb=' +
            cb
    );
    var actUrl = rtMonitorApiUrl(
        '/api/fina_stock_basic_act?ts_code=' + encodeURIComponent(normalized) + '&_cb=' + cb
    );
    /* 实控人/企业性质：与序列并行拉取专用接口再合并，避免「主包缺字段/缓存/条件判断漏补」导致顶栏恒为 -- */
    Promise.all([
        fetch(url, { cache: 'no-store' }).then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, data: data };
            });
        }),
        fetch(actUrl, { cache: 'no-store' })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .catch(function () {
                return { ok: false, data: null };
            }),
    ])
        .then(function (pair) {
            var o = pair[0];
            var actPack = pair[1];
            if (!o.ok || !o.data || o.data.error) {
                var err = o && o.data && o.data.error ? String(o.data.error) : '加载失败';
                clearFinaFixedAssetsMetricsBar();
                clearFinaFixedAssetsMainbzTable();
                clearFinaFixedAssetsOperatingDataTable();
                finaFixedAssetsCurrentRows = [];
                finaFixedAssetsCurrentTitle = '';
                showFinaFixedAssetsMsg(err);
                syncFinaFixedAssetsNavButtons();
                return;
            }
            /* 先收起「加载中…」避免遮罩盖住指标条；_cb 已在 URL 上削弱中间层缓存 */
            showFinaFixedAssetsMsg('');
            var merged = o.data;
            if (actPack && actPack.ok && actPack.data && !actPack.data.error) {
                var aj = actPack.data;
                if (aj.act_name != null && String(aj.act_name).trim() !== '') {
                    merged.act_name = aj.act_name;
                }
                if (aj.act_ent_type != null && String(aj.act_ent_type).trim() !== '') {
                    merged.act_ent_type = aj.act_ent_type;
                }
            }
            var sn = merged.sec_name != null ? String(merged.sec_name) : '';
            var arturnRows = Array.isArray(_finaMetricState().rows) ? _finaMetricState().rows : [];
            var arturnRow = null;
            for (var ri = 0; ri < arturnRows.length; ri += 1) {
                var rr = arturnRows[ri] || {};
                if (normalizeFinaFixedAssetsTsCode(rr.ts_code) === normalized) {
                    arturnRow = rr;
                    break;
                }
            }
            updateFinaFixedAssetsHeaderLine(normalized, sn);
            renderFinaFixedAssetsMetricsBar(merged, arturnRow);
            renderFinaFixedAssetsMainbzTable(merged);
            var rowArr = Array.isArray(merged.rows) ? merged.rows : [];
            finaFixedAssetsCurrentRows = rowArr;
            finaFixedAssetsCurrentTitle = (sn || normalized || '').trim();
            renderFinaFixedAssetsChart(rowArr, normalized);
            renderFinaFixedAssetsOperatingDataTable(rowArr);
            syncFinaFixedAssetsNavButtons();
        })
        .catch(function (err) {
            clearFinaFixedAssetsMetricsBar();
            clearFinaFixedAssetsMainbzTable();
            clearFinaFixedAssetsOperatingDataTable();
            finaFixedAssetsCurrentRows = [];
            finaFixedAssetsCurrentTitle = '';
            showFinaFixedAssetsMsg('请求失败：' + String(err && err.message ? err.message : err));
            syncFinaFixedAssetsNavButtons();
        });
}

function openFinaFixedAssetsModal(tsCode, secName) {
    var overlay = document.getElementById('fina-fixed-assets-modal-overlay');
    if (!overlay) return;
    finaFixedAssetsModalLayoutOpenTs = Date.now();
    var raw = String(tsCode || '').trim();
    var normalized = normalizeFinaFixedAssetsTsCode(raw);
    var inp = document.getElementById('fina-fixed-assets-ts-input');
    if (inp) {
        inp.value = formatFinaFixedAssetsTsCodeDisplay(normalized || raw);
    }
    updateFinaFixedAssetsHeaderLine(normalized || raw, secName || '');
    overlay.classList.add('is-open');
    _modalOpened();
    applyFinaFixedAssetsModalSavedLayout();
    var selectedEndDate = String(_finaMetricState().selectedEndDate || '').trim();
    if (!selectedEndDate) {
        showFinaFixedAssetsMsg('当前报告期为空，无法加载图表');
        syncFinaFixedAssetsNavButtons();
        return;
    }
    fetchFinaFixedAssetsSeries(inp ? inp.value : raw);
    syncFinaFixedAssetsNavButtons();
}

/** 经营现金流比率列：年报 o0..o3 接口为库内小数×100 后的百分数；单季 ocs/oco 为库内直接值。页面均固定两位小数。 */
function formatFinaArturnOcfOrPct(v) {
    if (v === null || v === undefined || v === '') return '--';
    var n = parseFloat(v);
    if (isNaN(n)) return '--';
    return n.toFixed(2);
}

function formatFinaArturnAnnDate(v) {
    var s = String(v == null ? '' : v).trim();
    if (!s) return '--';
    if (/^\d{8}$/.test(s)) return s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8);
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    return s;
}

/** 上市日至「今天」的日历年、月数（与弹窗指标「已上市…」同口径） */
function finaListingTenureZhFromNow(listDateRaw) {
    var s = String(listDateRaw == null ? '' : listDateRaw).trim();
    if (!s) return '';
    var y;
    var mo;
    var d;
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
        y = parseInt(s.slice(0, 4), 10);
        mo = parseInt(s.slice(5, 7), 10);
        d = parseInt(s.slice(8, 10), 10);
    } else if (/^\d{8}$/.test(s)) {
        y = parseInt(s.slice(0, 4), 10);
        mo = parseInt(s.slice(4, 6), 10);
        d = parseInt(s.slice(6, 8), 10);
    } else {
        return '';
    }
    if (isNaN(y) || isNaN(mo) || isNaN(d)) return '';
    var start = new Date(y, mo - 1, d);
    if (isNaN(start.getTime())) return '';
    var end = new Date();
    end.setHours(0, 0, 0, 0);
    start.setHours(0, 0, 0, 0);
    if (end < start) return '';
    var yy = end.getFullYear() - start.getFullYear();
    var mm = end.getMonth() - start.getMonth();
    var dd = end.getDate() - start.getDate();
    if (dd < 0) {
        mm -= 1;
    }
    if (mm < 0) {
        yy -= 1;
        mm += 12;
    }
    if (yy < 0) {
        yy = 0;
        mm = 0;
    }
    if (yy === 0 && mm === 0) {
        return '已上市不足1个月';
    }
    if (yy === 0) {
        return '已上市' + mm + '个月';
    }
    if (mm === 0) {
        return '已上市' + yy + '年';
    }
    return '已上市' + yy + '年' + mm + '个月';
}

/** 财务数值列为负时加类名，供绿色字色（ROE / 经营现金流两组） */
function finaArturnNegClass(v) {
    if (v === null || v === undefined || v === '') return '';
    var n = parseFloat(v);
    if (isNaN(n)) return '';
    return n < 0 ? ' fina-arturn-neg' : '';
}

function buildNumericFilterMatcher(exprRaw) {
    var expr = String(exprRaw || '').trim();
    if (!expr) return null;
    var opMatch = expr.match(/^(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)$/);
    if (opMatch) {
        var op = opMatch[1];
        var val = parseFloat(opMatch[2]);
        if (isNaN(val)) return null;
        return function (v) {
            var n = parseFloat(v);
            if (isNaN(n)) return false;
            if (op === '>=') return n >= val;
            if (op === '<=') return n <= val;
            if (op === '>') return n > val;
            if (op === '<') return n < val;
            return n === val;
        };
    }
    var rangeMatch = expr.match(/^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$/);
    if (rangeMatch) {
        var minV = parseFloat(rangeMatch[1]);
        var maxV = parseFloat(rangeMatch[2]);
        if (isNaN(minV) || isNaN(maxV)) return null;
        var lo = Math.min(minV, maxV);
        var hi = Math.max(minV, maxV);
        return function (v) {
            var n = parseFloat(v);
            if (isNaN(n)) return false;
            return n >= lo && n <= hi;
        };
    }
    var eq = parseFloat(expr);
    if (!isNaN(eq)) {
        return function (v) {
            var n = parseFloat(v);
            if (isNaN(n)) return false;
            return n === eq;
        };
    }
    return null;
}

function loadFinaMetricSchemes(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return {};
    try {
        if (typeof localStorage === 'undefined') return {};
        var raw = localStorage.getItem(cfg.schemeStorageKey);
        if (!raw) return {};
        var obj = JSON.parse(raw);
        return obj && typeof obj === 'object' ? obj : {};
    } catch (e) {
        return {};
    }
}

function saveFinaMetricSchemes(panelKey, allSchemes) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    try {
        if (typeof localStorage === 'undefined') return;
        localStorage.setItem(cfg.schemeStorageKey, JSON.stringify(allSchemes || {}));
    } catch (e) {
        logError('保存筛选方案失败 ' + panelKey, e);
    }
}

function loadFinaArturnSchemes() {
    return loadFinaMetricSchemes('arturn');
}

function saveFinaArturnSchemes(allSchemes) {
    saveFinaMetricSchemes('arturn', allSchemes);
}

function renderFinaMetricSchemeOptions(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var sel = document.getElementById(cfg.ids.schemeSelect);
    if (!sel) return;
    var schemes = loadFinaMetricSchemes(panelKey);
    var names = Object.keys(schemes).sort();
    var html = ['<option value="">选择已保存方案</option>'];
    names.forEach(function (name) {
        html.push('<option value="' + escapeHtmlAttr(name) + '">' + escapeHtmlAttr(name) + '</option>');
    });
    sel.innerHTML = html.join('');
    refreshFinaMetricSchemeSummary(panelKey);
}

function renderFinaArturnSchemeOptions() {
    renderFinaMetricSchemeOptions('arturn');
}

function getFinaMetricFilters(panelKey) {
    return Object.assign({}, _finaMetricState().filters || {});
}

/** 与 setFinaMetricFilters 同源，供 localStorage 恢复时补齐新增键且不依赖未定义函数 */
function buildFinaMetricFilterShapeFrom(src) {
    var s = src || {};
    return {
        tsCode: String(s.tsCode || ''),
        secName: String(s.secName || ''),
        swName: String(s.swName || ''),
        total_mv_yi: String(s.total_mv_yi || ''),
        free_mv_yi: String(s.free_mv_yi || ''),
        a0: String(s.a0 || ''),
        listDate: String(s.listDate || ''),
        money_cap_ratio_pct: String(s.money_cap_ratio_pct || ''),
        fqm_debt_to_assets: String(s.fqm_debt_to_assets || ''),
        fqm_interestdebt_ratio: String(s.fqm_interestdebt_ratio || ''),
        ttm_revenue_yi: String(s.ttm_revenue_yi || ''),
        pe_ttm: String(s.pe_ttm || ''),
        ar_pct: String(s.ar_pct || ''),
        roe0: String(s.roe0 || ''),
        roe1: String(s.roe1 || ''),
        roe2: String(s.roe2 || ''),
        roe3: String(s.roe3 || ''),
        p0: String(s.p0 || ''),
        p1: String(s.p1 || ''),
        p2: String(s.p2 || ''),
        p3: String(s.p3 || ''),
        g0: String(s.g0 || ''),
        g1: String(s.g1 || ''),
        g2: String(s.g2 || ''),
        g3: String(s.g3 || ''),
        c0: String(s.c0 || ''),
        c1: String(s.c1 || ''),
        c2: String(s.c2 || ''),
        c3: String(s.c3 || ''),
        o0: String(s.o0 || ''),
        o1: String(s.o1 || ''),
        o2: String(s.o2 || ''),
        o3: String(s.o3 || ''),
        s_high: String(s.s_high || ''),
        cf_high: String(s.cf_high || ''),
        cf0: String(s.cf0 || ''),
        cf1: String(s.cf1 || ''),
        cf2: String(s.cf2 || ''),
        cf3: String(s.cf3 || ''),
        cf4: String(s.cf4 || ''),
        cf5: String(s.cf5 || ''),
        cf6: String(s.cf6 || ''),
        cf7: String(s.cf7 || ''),
        cf8: String(s.cf8 || ''),
        oi_high: String(s.oi_high || ''),
        oi0: String(s.oi0 || ''),
        oi1: String(s.oi1 || ''),
        oi2: String(s.oi2 || ''),
        oi3: String(s.oi3 || ''),
        oi4: String(s.oi4 || ''),
        oi5: String(s.oi5 || ''),
        oi6: String(s.oi6 || ''),
        oi7: String(s.oi7 || ''),
        oi8: String(s.oi8 || ''),
        dtp_high: String(s.dtp_high || ''),
        dtp0: String(s.dtp0 || ''),
        dtp1: String(s.dtp1 || ''),
        dtp2: String(s.dtp2 || ''),
        dtp3: String(s.dtp3 || ''),
        dtp4: String(s.dtp4 || ''),
        dtp5: String(s.dtp5 || ''),
        dtp6: String(s.dtp6 || ''),
        dtp7: String(s.dtp7 || ''),
        dtp8: String(s.dtp8 || ''),
        fst_high: String(s.fst_high || ''),
        fst0: String(s.fst0 || ''),
        fst1: String(s.fst1 || ''),
        fst2: String(s.fst2 || ''),
        fst3: String(s.fst3 || ''),
        fst4: String(s.fst4 || ''),
        fst5: String(s.fst5 || ''),
        fst6: String(s.fst6 || ''),
        fst7: String(s.fst7 || ''),
        fst8: String(s.fst8 || ''),
        ocs_high: String(s.ocs_high || ''),
        ocs_wqcv: String(s.ocs_wqcv || ''),
        ocs0: String(s.ocs0 || ''),
        ocs1: String(s.ocs1 || ''),
        ocs2: String(s.ocs2 || ''),
        ocs3: String(s.ocs3 || ''),
        ocs4: String(s.ocs4 || ''),
        ocs5: String(s.ocs5 || ''),
        ocs6: String(s.ocs6 || ''),
        ocs7: String(s.ocs7 || ''),
        ocs8: String(s.ocs8 || ''),
        oco0: String(s.oco0 || ''),
        oco1: String(s.oco1 || ''),
        oco2: String(s.oco2 || ''),
        oco3: String(s.oco3 || ''),
        oco4: String(s.oco4 || ''),
        oco5: String(s.oco5 || ''),
        oco6: String(s.oco6 || ''),
        oco7: String(s.oco7 || ''),
        oco8: String(s.oco8 || ''),
        r0: String(s.r0 || ''),
        r1: String(s.r1 || ''),
        r2: String(s.r2 || ''),
        r3: String(s.r3 || ''),
        r4: String(s.r4 || ''),
        r5: String(s.r5 || ''),
        r6: String(s.r6 || ''),
        r7: String(s.r7 || ''),
        r8: String(s.r8 || ''),
        m_high: String(s.m_high || ''),
        m_qoq: String(s.m_qoq || ''),
        m0: String(s.m0 || ''),
        m1: String(s.m1 || ''),
        m2: String(s.m2 || ''),
        m3: String(s.m3 || ''),
        m4: String(s.m4 || ''),
        m5: String(s.m5 || ''),
        m6: String(s.m6 || ''),
        m7: String(s.m7 || ''),
        m8: String(s.m8 || ''),
        n0: String(s.n0 || ''),
        n1: String(s.n1 || ''),
        n2: String(s.n2 || ''),
        n3: String(s.n3 || ''),
        n4: String(s.n4 || ''),
        n5: String(s.n5 || ''),
        n6: String(s.n6 || ''),
        n7: String(s.n7 || ''),
        n8: String(s.n8 || ''),
        t0: String(s.t0 || ''),
        t1: String(s.t1 || ''),
        t2: String(s.t2 || ''),
        t3: String(s.t3 || ''),
        t4: String(s.t4 || ''),
        t5: String(s.t5 || ''),
        t6: String(s.t6 || ''),
        t7: String(s.t7 || ''),
        t8: String(s.t8 || ''),
        invchg: String(s.invchg || ''),
        inv_revenue_pct: String(s.inv_revenue_pct || ''),
        inv_high: String(s.inv_high || ''),
        inv0: String(s.inv0 || ''),
        inv1: String(s.inv1 || ''),
        inv2: String(s.inv2 || ''),
        inv3: String(s.inv3 || ''),
        inv4: String(s.inv4 || ''),
        inv5: String(s.inv5 || ''),
        inv6: String(s.inv6 || ''),
        inv7: String(s.inv7 || ''),
        inv8: String(s.inv8 || ''),
        kchg: String(s.kchg || ''),
        k_revenue_pct: String(s.k_revenue_pct || ''),
        k_high: String(s.k_high || ''),
        k0: String(s.k0 || ''),
        k1: String(s.k1 || ''),
        k2: String(s.k2 || ''),
        k3: String(s.k3 || ''),
        k4: String(s.k4 || ''),
        k5: String(s.k5 || ''),
        k6: String(s.k6 || ''),
        k7: String(s.k7 || ''),
        k8: String(s.k8 || ''),
        fchg: String(s.fchg || ''),
        f0: String(s.f0 || ''),
        f1: String(s.f1 || ''),
        f2: String(s.f2 || ''),
        f3: String(s.f3 || ''),
        f4: String(s.f4 || ''),
        f5: String(s.f5 || ''),
        f6: String(s.f6 || ''),
        f7: String(s.f7 || ''),
        f8: String(s.f8 || ''),
        u0: String(s.u0 || ''),
        u1: String(s.u1 || ''),
        u2: String(s.u2 || ''),
        u3: String(s.u3 || ''),
        u4: String(s.u4 || ''),
        u5: String(s.u5 || ''),
        u6: String(s.u6 || ''),
        u7: String(s.u7 || ''),
        u8: String(s.u8 || ''),
        v0: String(s.v0 || ''),
        v1: String(s.v1 || ''),
        v2: String(s.v2 || ''),
        v3: String(s.v3 || ''),
        v4: String(s.v4 || ''),
        v5: String(s.v5 || ''),
        v6: String(s.v6 || ''),
        v7: String(s.v7 || ''),
        v8: String(s.v8 || '')
    };
}

function normalizeFinaMetricFilterShape(raw) {
    return buildFinaMetricFilterShapeFrom(raw);
}

function setFinaMetricFilters(panelKey, filters) {
    _finaMetricState().filters = buildFinaMetricFilterShapeFrom(filters);
}

function getCurrentFinaArturnFilters() {
    return getFinaMetricFilters('arturn');
}

function setCurrentFinaArturnFilters(filters) {
    setFinaMetricFilters('arturn', filters);
}

function renderFinaMetricHeaderFilterButtons(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var filters = getFinaMetricFilters(panelKey);
    /* 同时更新可滚动面板和冻结面板的筛选按钮激活状态 */
    var selectors = '#' + cfg.ids.thead + ' .fina-filter-btn';
    if (panelKey === 'arturn') {
        selectors += ', #fina-arturn-frozen-thead .fina-filter-btn';
    }
    var btns = document.querySelectorAll(selectors);
    btns.forEach(function (btn) {
        var key = btn.getAttribute('data-filter-key') || '';
        var hasVal = String(filters[key] || '').trim() !== '';
        btn.classList.toggle('is-active', hasVal);
    });
}

function renderFinaArturnHeaderFilterButtons() {
    renderFinaMetricHeaderFilterButtons('arturn');
}

function renderFinaMetricEndDateOptions(panelKey, endDates, selected) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var sel = document.getElementById(cfg.ids.schemeEndDate);
    if (!sel) return;
    var arr = Array.isArray(endDates) ? endDates : [];
    var html = [];
    arr.forEach(function (d) {
        html.push('<option value="' + escapeHtmlAttr(d) + '">' + escapeHtmlAttr(d) + '</option>');
    });
    sel.innerHTML = html.join('');
    if (selected && arr.indexOf(selected) >= 0) {
        sel.value = selected;
    }
}

function renderFinaArturnEndDateOptions(endDates, selected) {
    renderFinaMetricEndDateOptions('arturn', endDates, selected);
}

function loadFinaMetricSchemeApplyPref(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return null;
    try {
        if (typeof localStorage === 'undefined') return null;
        var raw = localStorage.getItem(FINA_ARTURN_SCHEME_APPLY_PREF_KEY);
        if (!raw) return null;
        var data = JSON.parse(raw);
        if (!data || typeof data !== 'object') return null;
        var mode = String(data.mode || '');
        return {
            mode: (mode === 'scheme' || mode === 'manual') ? mode : 'current',
            manualEndDate: String(data.manualEndDate || '')
        };
    } catch (e) {
        return null;
    }
}

function saveFinaMetricSchemeApplyPref(panelKey, payload) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    try {
        if (typeof localStorage === 'undefined') return;
        var mode = String(payload && payload.mode || 'current');
        var manualEndDate = String(payload && payload.manualEndDate || '');
        localStorage.setItem(FINA_ARTURN_SCHEME_APPLY_PREF_KEY, JSON.stringify({
            mode: (mode === 'scheme' || mode === 'manual') ? mode : 'current',
            manualEndDate: manualEndDate
        }));
    } catch (e) {
        /* ignore localStorage failures */
    }
}

function renderFinaMetricSchemeApplyManualEndDateOptions(panelKey, endDates, selected) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var sel = document.getElementById(cfg.ids.schemeApplyEndDateSelect);
    if (!sel) return;
    var arr = Array.isArray(endDates) ? endDates : [];
    var html = [];
    arr.forEach(function (d) {
        html.push('<option value="' + escapeHtmlAttr(d) + '">' + escapeHtmlAttr(d) + '</option>');
    });
    sel.innerHTML = html.join('');
    var prefEndDate = String(sel.dataset.prefEndDate || '');
    if (prefEndDate && arr.indexOf(prefEndDate) >= 0) {
        sel.value = prefEndDate;
        sel.dataset.prefEndDate = '';
    } else if (selected && arr.indexOf(selected) >= 0) {
        sel.value = selected;
    } else if (arr.length > 0) {
        sel.value = arr[0];
    }
}

function syncFinaMetricSchemeApplyModeUi(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var modeSel = document.getElementById(cfg.ids.schemeApplyEndDateMode);
    var manualWrap = document.getElementById(cfg.ids.schemeApplyEndDateManualWrap);
    if (!modeSel || !manualWrap) return;
    manualWrap.hidden = String(modeSel.value || 'current') !== 'manual';
}

/** 列头展示：YYYYMMDD → 四位年份；其它保持原文（接口与筛选键仍用完整报告期） */
/** 顶栏「更新：」仅显示日期 YYYY-MM-DD（去掉时间） */
function finaArturnFormatHeadDate(raw) {
    if (raw == null || raw === '') return '--';
    var s = String(raw).trim();
    if (!s || s === '--') return '--';
    var m = s.match(/^(\d{4}-\d{2}-\d{2})/);
    return m ? m[1] : s;
}

function finaArturnPeriodDisplayYear(periodStr) {
    var s = String(periodStr || '').trim();
    if (/^\d{8}$/.test(s)) return s.slice(0, 4);
    return s || '--';
}

function finaArturnQuarterLabel(periodStr) {
    var s = String(periodStr || '').trim();
    if (!/^\d{8}$/.test(s)) return s || '--';
    var yy = s.slice(2, 4);
    var mmdd = s.slice(4, 8);
    var q = '--';
    if (mmdd === '0331') q = 'Q1';
    else if (mmdd === '0630') q = 'Q2';
    else if (mmdd === '0930') q = 'Q3';
    else if (mmdd === '1231') q = 'Q4';
    return yy + q;
}

function finaArturnDigitsOnly(v) {
    return String(v == null ? '' : v).replace(/\D+/g, '');
}

function finaArturnDateTextMatch(rawVal, kwRaw) {
    var valTxt = String(rawVal || '').trim().toLowerCase();
    var kwTxt = String(kwRaw || '').trim().toLowerCase();
    if (!kwTxt) return true;
    if (valTxt.indexOf(kwTxt) >= 0) return true;
    var valDigits = finaArturnDigitsOnly(rawVal);
    var kwDigits = finaArturnDigitsOnly(kwRaw);
    if (kwDigits && valDigits && valDigits.indexOf(kwDigits) >= 0) return true;
    return false;
}

function computeContractLiabChg(row) {
    var cur = parseFloat(row && row.k0);
    if (isNaN(cur)) return null;
    var histVals = [];
    for (var i = 1; i <= 8; i += 1) {
        var hv = parseFloat(row && row['k' + i]);
        if (!isNaN(hv)) histVals.push(hv);
    }
    if (!histVals.length) return null;
    var sum = 0;
    for (var j = 0; j < histVals.length; j += 1) sum += histVals[j];
    var avg = sum / histVals.length;
    if (!isFinite(avg) || avg === 0) return null;
    return ((cur / avg) - 1) * 100;
}

function computeInventoryChg(row) {
    var cur = parseFloat(row && row.inv0);
    if (isNaN(cur)) return null;
    var histVals = [];
    for (var i = 1; i <= 8; i += 1) {
        var hv = parseFloat(row && row['inv' + i]);
        if (!isNaN(hv)) histVals.push(hv);
    }
    if (!histVals.length) return null;
    var sum = 0;
    for (var j = 0; j < histVals.length; j += 1) sum += histVals[j];
    var avg = sum / histVals.length;
    if (!isFinite(avg) || avg === 0) return null;
    return ((cur / avg) - 1) * 100;
}

function computeFixedAssetsChg(row) {
    var cur = parseFloat(row && row.f0);
    if (isNaN(cur)) return null;
    var histVals = [];
    for (var i = 1; i <= 8; i += 1) {
        var hv = parseFloat(row && row['f' + i]);
        if (!isNaN(hv)) histVals.push(hv);
    }
    if (!histVals.length) return null;
    var sum = 0;
    for (var j = 0; j < histVals.length; j += 1) sum += histVals[j];
    var avg = sum / histVals.length;
    if (!isFinite(avg) || avg === 0) return null;
    return ((cur / avg) - 1) * 100;
}

function finaArturnGetSortCell(row, key) {
    if (key === 'tsCode') return row.ts_code;
    if (key === 'secName') return row.sec_name;
    if (key === 'swName') return row.sw_name;
    if (key === 'total_mv_yi') return row.total_mv_yi;
    if (key === 'free_mv_yi') return row.free_mv_yi;
    if (key === 'a0') return row.a0;
    if (key === 'listDate') return row.list_date;
    if (key === 'money_cap_ratio_pct') return row.money_cap_ratio_pct;
    if (key === 'fqm_debt_to_assets') return row.fqm_debt_to_assets;
    if (key === 'fqm_interestdebt_ratio') return row.fqm_interestdebt_ratio;
    if (key === 'ttm_revenue_yi') return row.ttm_revenue_yi;
    if (key === 'pe_ttm') return row.pe_ttm;
    if (key === 'ar_pct') return row.ar_pct;
    if (key === 'roe0') return row.roe0;
    if (key === 'roe1') return row.roe1;
    if (key === 'roe2') return row.roe2;
    if (key === 'roe3') return row.roe3;
    if (key === 'p0') return row.p0;
    if (key === 'p1') return row.p1;
    if (key === 'p2') return row.p2;
    if (key === 'p3') return row.p3;
    if (key === 'g0') return row.g0;
    if (key === 'g1') return row.g1;
    if (key === 'g2') return row.g2;
    if (key === 'g3') return row.g3;
    if (key === 'c0') return row.c0;
    if (key === 'c1') return row.c1;
    if (key === 'c2') return row.c2;
    if (key === 'c3') return row.c3;
    if (key === 'o0') return row.o0;
    if (key === 'o1') return row.o1;
    if (key === 'o2') return row.o2;
    if (key === 'o3') return row.o3;
    if (key === 's_high') return row.s_high;
    if (key === 'cf_high') return row.cf_high;
    if (key === 'oi_high') return row.oi_high;
    if (key === 'dtp_high') return row.dtp_high;
    if (key === 'fst_high') return row.fst_high;
    if (key === 'ocs_high') return row.ocs_high;
    if (key === 'ocs_wqcv') return row.ocs_wqcv;
    if (key === 'k_high') return row.k_high;
    if (key === 's0') return row.s0;
    if (key === 's1') return row.s1;
    if (key === 's2') return row.s2;
    if (key === 's3') return row.s3;
    if (key === 's4') return row.s4;
    if (key === 's5') return row.s5;
    if (key === 's6') return row.s6;
    if (key === 's7') return row.s7;
    if (key === 's8') return row.s8;
    if (key === 'cf0') return row.cf0;
    if (key === 'cf1') return row.cf1;
    if (key === 'cf2') return row.cf2;
    if (key === 'cf3') return row.cf3;
    if (key === 'cf4') return row.cf4;
    if (key === 'cf5') return row.cf5;
    if (key === 'cf6') return row.cf6;
    if (key === 'cf7') return row.cf7;
    if (key === 'cf8') return row.cf8;
    if (key === 'oi0') return row.oi0;
    if (key === 'oi1') return row.oi1;
    if (key === 'oi2') return row.oi2;
    if (key === 'oi3') return row.oi3;
    if (key === 'oi4') return row.oi4;
    if (key === 'oi5') return row.oi5;
    if (key === 'oi6') return row.oi6;
    if (key === 'oi7') return row.oi7;
    if (key === 'oi8') return row.oi8;
    if (key === 'dtp0') return row.dtp0;
    if (key === 'dtp1') return row.dtp1;
    if (key === 'dtp2') return row.dtp2;
    if (key === 'dtp3') return row.dtp3;
    if (key === 'dtp4') return row.dtp4;
    if (key === 'dtp5') return row.dtp5;
    if (key === 'dtp6') return row.dtp6;
    if (key === 'dtp7') return row.dtp7;
    if (key === 'dtp8') return row.dtp8;
    if (key === 'fst0') return row.fst0;
    if (key === 'fst1') return row.fst1;
    if (key === 'fst2') return row.fst2;
    if (key === 'fst3') return row.fst3;
    if (key === 'fst4') return row.fst4;
    if (key === 'fst5') return row.fst5;
    if (key === 'fst6') return row.fst6;
    if (key === 'fst7') return row.fst7;
    if (key === 'fst8') return row.fst8;
    if (key === 'ocs0') return row.ocs0;
    if (key === 'ocs1') return row.ocs1;
    if (key === 'ocs2') return row.ocs2;
    if (key === 'ocs3') return row.ocs3;
    if (key === 'ocs4') return row.ocs4;
    if (key === 'ocs5') return row.ocs5;
    if (key === 'ocs6') return row.ocs6;
    if (key === 'ocs7') return row.ocs7;
    if (key === 'ocs8') return row.ocs8;
    if (key === 'oco0') return row.oco0;
    if (key === 'oco1') return row.oco1;
    if (key === 'oco2') return row.oco2;
    if (key === 'oco3') return row.oco3;
    if (key === 'oco4') return row.oco4;
    if (key === 'oco5') return row.oco5;
    if (key === 'oco6') return row.oco6;
    if (key === 'oco7') return row.oco7;
    if (key === 'oco8') return row.oco8;
    if (key === 'r0') return row.r0;
    if (key === 'r1') return row.r1;
    if (key === 'r2') return row.r2;
    if (key === 'r3') return row.r3;
    if (key === 'r4') return row.r4;
    if (key === 'r5') return row.r5;
    if (key === 'r6') return row.r6;
    if (key === 'r7') return row.r7;
    if (key === 'r8') return row.r8;
    if (key === 'm_high') return row.m_high;
    if (key === 'm_qoq') return row.m_qoq;
    if (key === 'm0') return row.m0;
    if (key === 'm1') return row.m1;
    if (key === 'm2') return row.m2;
    if (key === 'm3') return row.m3;
    if (key === 'm4') return row.m4;
    if (key === 'm5') return row.m5;
    if (key === 'm6') return row.m6;
    if (key === 'm7') return row.m7;
    if (key === 'm8') return row.m8;
    if (key === 'n0') return row.n0;
    if (key === 'n1') return row.n1;
    if (key === 'n2') return row.n2;
    if (key === 'n3') return row.n3;
    if (key === 'n4') return row.n4;
    if (key === 'n5') return row.n5;
    if (key === 'n6') return row.n6;
    if (key === 'n7') return row.n7;
    if (key === 'n8') return row.n8;
    if (key === 't0') return row.t0;
    if (key === 't1') return row.t1;
    if (key === 't2') return row.t2;
    if (key === 't3') return row.t3;
    if (key === 't4') return row.t4;
    if (key === 't5') return row.t5;
    if (key === 't6') return row.t6;
    if (key === 't7') return row.t7;
    if (key === 't8') return row.t8;
    if (key === 'invchg') return computeInventoryChg(row);
    if (key === 'inv_revenue_pct') return row.inv_revenue_pct;
    if (key === 'inv_high') return row.inv_high;
    if (key === 'inv0') return row.inv0;
    if (key === 'inv1') return row.inv1;
    if (key === 'inv2') return row.inv2;
    if (key === 'inv3') return row.inv3;
    if (key === 'inv4') return row.inv4;
    if (key === 'inv5') return row.inv5;
    if (key === 'inv6') return row.inv6;
    if (key === 'inv7') return row.inv7;
    if (key === 'inv8') return row.inv8;
    if (key === 'kchg') return computeContractLiabChg(row);
    if (key === 'k_revenue_pct') return row.k_revenue_pct;
    if (key === 'k0') return row.k0;
    if (key === 'k1') return row.k1;
    if (key === 'k2') return row.k2;
    if (key === 'k3') return row.k3;
    if (key === 'k4') return row.k4;
    if (key === 'k5') return row.k5;
    if (key === 'k6') return row.k6;
    if (key === 'k7') return row.k7;
    if (key === 'k8') return row.k8;
    if (key === 'fchg') return computeFixedAssetsChg(row);
    if (key === 'f0') return row.f0;
    if (key === 'f1') return row.f1;
    if (key === 'f2') return row.f2;
    if (key === 'f3') return row.f3;
    if (key === 'f4') return row.f4;
    if (key === 'f5') return row.f5;
    if (key === 'f6') return row.f6;
    if (key === 'f7') return row.f7;
    if (key === 'f8') return row.f8;
    if (key === 'u0') return row.u0;
    if (key === 'u1') return row.u1;
    if (key === 'u2') return row.u2;
    if (key === 'u3') return row.u3;
    if (key === 'u4') return row.u4;
    if (key === 'u5') return row.u5;
    if (key === 'u6') return row.u6;
    if (key === 'u7') return row.u7;
    if (key === 'u8') return row.u8;
    if (key === 'v0') return row.v0;
    if (key === 'v1') return row.v1;
    if (key === 'v2') return row.v2;
    if (key === 'v3') return row.v3;
    if (key === 'v4') return row.v4;
    if (key === 'v5') return row.v5;
    if (key === 'v6') return row.v6;
    if (key === 'v7') return row.v7;
    if (key === 'v8') return row.v8;
    return '';
}

/** 数值列排序用：无效值按 asc 置后 / desc 置后 */
function finaArturnNumSortProxy(v, dir) {
    var n = parseFloat(v);
    if (!isNaN(n)) return n;
    return dir === 'asc' ? Infinity : -Infinity;
}

function finaArturnCompareRows(a, b, key, dir) {
    var mult = dir === 'desc' ? -1 : 1;
    if (key === 'tsCode' || key === 'secName' || key === 'swName') {
        var sa = String(finaArturnGetSortCell(a, key) || '');
        var sb = String(finaArturnGetSortCell(b, key) || '');
        return mult * sa.localeCompare(sb, 'zh-CN', { numeric: true, sensitivity: 'base' });
    }
    if (key === 'a0' || key === 'listDate') {
        var da = String(finaArturnGetSortCell(a, key) || '');
        var db = String(finaArturnGetSortCell(b, key) || '');
        return mult * da.localeCompare(db, 'zh-CN', { numeric: true, sensitivity: 'base' });
    }
    if (key === 'ar_pct' || key === 'money_cap_ratio_pct' || key === 'fqm_debt_to_assets' ||
            key === 'fqm_interestdebt_ratio' || key === 'ttm_revenue_yi' || key === 'pe_ttm' ||
            key === 'total_mv_yi' || key === 'free_mv_yi' ||
            key === 'roe0' || key === 'roe1' || key === 'roe2' || key === 'roe3' ||
            key === 'p0' || key === 'p1' || key === 'p2' || key === 'p3' ||
            key === 'g0' || key === 'g1' || key === 'g2' || key === 'g3' ||
            key === 'c0' || key === 'c1' || key === 'c2' || key === 'c3' ||
            key === 'o0' || key === 'o1' || key === 'o2' || key === 'o3' ||
            key === 's_high' ||
            key === 'cf_high' ||
            key === 'oi_high' ||
            key === 'dtp_high' ||
            key === 'fst_high' ||
            key === 'ocs_high' ||
            key === 'ocs_wqcv' ||
            key === 'k_high' ||
            key === 's0' || key === 's1' || key === 's2' || key === 's3' || key === 's4' ||
            key === 's5' || key === 's6' || key === 's7' || key === 's8' ||
            key === 'cf0' || key === 'cf1' || key === 'cf2' || key === 'cf3' || key === 'cf4' ||
            key === 'cf5' || key === 'cf6' || key === 'cf7' || key === 'cf8' ||
            key === 'oi0' || key === 'oi1' || key === 'oi2' || key === 'oi3' || key === 'oi4' ||
            key === 'oi5' || key === 'oi6' || key === 'oi7' || key === 'oi8' ||
            key === 'dtp0' || key === 'dtp1' || key === 'dtp2' || key === 'dtp3' || key === 'dtp4' ||
            key === 'dtp5' || key === 'dtp6' || key === 'dtp7' || key === 'dtp8' ||
            key === 'fst0' || key === 'fst1' || key === 'fst2' || key === 'fst3' || key === 'fst4' ||
            key === 'fst5' || key === 'fst6' || key === 'fst7' || key === 'fst8' ||
            key === 'ocs0' || key === 'ocs1' || key === 'ocs2' || key === 'ocs3' || key === 'ocs4' ||
            key === 'ocs5' || key === 'ocs6' || key === 'ocs7' || key === 'ocs8' ||
            key === 'oco0' || key === 'oco1' || key === 'oco2' || key === 'oco3' || key === 'oco4' ||
            key === 'oco5' || key === 'oco6' || key === 'oco7' || key === 'oco8' ||
            key === 'r0' || key === 'r1' || key === 'r2' || key === 'r3' || key === 'r4' ||
            key === 'r5' || key === 'r6' || key === 'r7' || key === 'r8' ||
            key === 'm_high' ||
            key === 'm_qoq' ||
            key === 'm0' || key === 'm1' || key === 'm2' || key === 'm3' || key === 'm4' ||
            key === 'm5' || key === 'm6' || key === 'm7' || key === 'm8' ||
            key === 'n0' || key === 'n1' || key === 'n2' || key === 'n3' || key === 'n4' ||
            key === 'n5' || key === 'n6' || key === 'n7' || key === 'n8' ||
            key === 't0' || key === 't1' || key === 't2' || key === 't3' || key === 't4' ||
            key === 't5' || key === 't6' || key === 't7' || key === 't8' ||
            key === 'invchg' ||
            key === 'inv_revenue_pct' ||
            key === 'inv_high' ||
            key === 'inv0' || key === 'inv1' || key === 'inv2' || key === 'inv3' ||
            key === 'inv4' || key === 'inv5' || key === 'inv6' || key === 'inv7' || key === 'inv8' ||
            key === 'kchg' ||
            key === 'k_revenue_pct' ||
            key === 'k0' || key === 'k1' || key === 'k2' || key === 'k3' ||
            key === 'k4' || key === 'k5' || key === 'k6' || key === 'k7' || key === 'k8' ||
            key === 'fchg' ||
            key === 'f0' || key === 'f1' || key === 'f2' || key === 'f3' ||
            key === 'f4' || key === 'f5' || key === 'f6' || key === 'f7' || key === 'f8' ||
            key === 'u0' || key === 'u1' || key === 'u2' || key === 'u3' || key === 'u4' ||
            key === 'u5' || key === 'u6' || key === 'u7' || key === 'u8' ||
            key === 'v0' || key === 'v1' || key === 'v2' || key === 'v3' || key === 'v4' ||
            key === 'v5' || key === 'v6' || key === 'v7' || key === 'v8') {
        var pa = finaArturnNumSortProxy(finaArturnGetSortCell(a, key), dir);
        var pb = finaArturnNumSortProxy(finaArturnGetSortCell(b, key), dir);
        return mult * (pa - pb);
    }
    return 0;
}

/** 仅申万、公告日期与财务数值列可排序（代码、名称无排序钮） */
function finaArturnIsSortableColumnKey(key) {
    return key === 'swName' ||
        key === 'a0' ||
        key === 'listDate' ||
        key === 'total_mv_yi' ||
        key === 'free_mv_yi' ||
        key === 'money_cap_ratio_pct' ||
        key === 'fqm_debt_to_assets' ||
        key === 'fqm_interestdebt_ratio' ||
        key === 'ttm_revenue_yi' ||
        key === 'pe_ttm' ||
        key === 'ar_pct' ||
        key === 'roe0' || key === 'roe1' || key === 'roe2' || key === 'roe3' ||
        key === 'p0' || key === 'p1' || key === 'p2' || key === 'p3' ||
        key === 'g0' || key === 'g1' || key === 'g2' || key === 'g3' ||
        key === 'c0' || key === 'c1' || key === 'c2' || key === 'c3' ||
        key === 'o0' || key === 'o1' || key === 'o2' || key === 'o3' ||
        key === 's_high' ||
        key === 'cf_high' ||
        key === 'oi_high' ||
        key === 'dtp_high' ||
        key === 'fst_high' ||
        key === 'ocs_high' ||
        key === 'ocs_wqcv' ||
        key === 'k_high' ||
        key === 's0' || key === 's1' || key === 's2' || key === 's3' || key === 's4' ||
        key === 's5' || key === 's6' || key === 's7' || key === 's8' ||
        key === 'cf0' || key === 'cf1' || key === 'cf2' || key === 'cf3' || key === 'cf4' ||
        key === 'cf5' || key === 'cf6' || key === 'cf7' || key === 'cf8' ||
        key === 'oi0' || key === 'oi1' || key === 'oi2' || key === 'oi3' || key === 'oi4' ||
        key === 'oi5' || key === 'oi6' || key === 'oi7' || key === 'oi8' ||
        key === 'dtp0' || key === 'dtp1' || key === 'dtp2' || key === 'dtp3' || key === 'dtp4' ||
        key === 'dtp5' || key === 'dtp6' || key === 'dtp7' || key === 'dtp8' ||
        key === 'fst0' || key === 'fst1' || key === 'fst2' || key === 'fst3' || key === 'fst4' ||
        key === 'fst5' || key === 'fst6' || key === 'fst7' || key === 'fst8' ||
        key === 'ocs0' || key === 'ocs1' || key === 'ocs2' || key === 'ocs3' || key === 'ocs4' ||
        key === 'ocs5' || key === 'ocs6' || key === 'ocs7' || key === 'ocs8' ||
        key === 'oco0' || key === 'oco1' || key === 'oco2' || key === 'oco3' || key === 'oco4' ||
        key === 'oco5' || key === 'oco6' || key === 'oco7' || key === 'oco8' ||
        key === 'r0' || key === 'r1' || key === 'r2' || key === 'r3' || key === 'r4' ||
        key === 'r5' || key === 'r6' || key === 'r7' || key === 'r8' ||
        key === 'm_high' ||
        key === 'm_qoq' ||
        key === 'm0' || key === 'm1' || key === 'm2' || key === 'm3' || key === 'm4' ||
        key === 'm5' || key === 'm6' || key === 'm7' || key === 'm8' ||
        key === 'n0' || key === 'n1' || key === 'n2' || key === 'n3' || key === 'n4' ||
        key === 'n5' || key === 'n6' || key === 'n7' || key === 'n8' ||
        key === 't0' || key === 't1' || key === 't2' || key === 't3' || key === 't4' ||
        key === 't5' || key === 't6' || key === 't7' || key === 't8' ||
        key === 'invchg' ||
        key === 'inv_revenue_pct' ||
        key === 'inv_high' ||
        key === 'inv0' || key === 'inv1' || key === 'inv2' || key === 'inv3' ||
        key === 'inv4' || key === 'inv5' || key === 'inv6' || key === 'inv7' || key === 'inv8' ||
        key === 'kchg' ||
        key === 'k_revenue_pct' ||
        key === 'k0' || key === 'k1' || key === 'k2' || key === 'k3' ||
        key === 'k4' || key === 'k5' || key === 'k6' || key === 'k7' || key === 'k8' ||
        key === 'fchg' ||
        key === 'f0' || key === 'f1' || key === 'f2' || key === 'f3' ||
        key === 'f4' || key === 'f5' || key === 'f6' || key === 'f7' || key === 'f8' ||
        key === 'u0' || key === 'u1' || key === 'u2' || key === 'u3' || key === 'u4' ||
        key === 'u5' || key === 'u6' || key === 'u7' || key === 'u8' ||
        key === 'v0' || key === 'v1' || key === 'v2' || key === 'v3' || key === 'v4' ||
        key === 'v5' || key === 'v6' || key === 'v7' || key === 'v8';
}

/** 单列排序按钮：无 → 升序 → 降序 → 无；换列则从升序开始 */
function finaMetricCycleSort(key) {
    var st = _finaMetricState();
    if (!key || !finaArturnIsSortableColumnKey(key)) return;
    if (st.sortKey !== key) {
        st.sortKey = key;
        st.sortDir = 'asc';
        return;
    }
    if (st.sortDir === 'asc') {
        st.sortDir = 'desc';
        return;
    }
    st.sortKey = '';
    st.sortDir = 'asc';
}

function finaArturnCycleSort(key) {
    finaMetricCycleSort(key);
}

/** 应收账款表 thead：两行结构见 .cursor/rules/rt-monitor-fina-arturn-layout.mdc（勿改 rowspan 行序） */
function _finaArturnEscCellText(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/**
 * 同步两个面板 thead 各行的高度（取最大值写入两边），再更新 CSS 变量供第二行 sticky top 使用。
 * 分栏架构中，冻结面板 metric-cell 与可滚动面板 group-header 的自然高度不同，
 * 不同步则两行表头垂直错位。
 */
function syncFinaArturnTheadStickyHeights() {
    var frozenThead = document.getElementById('fina-arturn-frozen-thead');
    var scrollThead = document.getElementById('fina-arturn-thead');
    if (!frozenThead || !scrollThead) return;

    /* 先清除上次设置的 height，让浏览器按内容重新计算自然高度 */
    [frozenThead, scrollThead].forEach(function (thead) {
        Array.from(thead.querySelectorAll('tr')).forEach(function (tr) {
            tr.style.height = '';
        });
    });

    /* 强制 reflow：读取高度前确保 style 已应用 */
    var fRows = Array.from(frozenThead.querySelectorAll('tr'));
    var sRows = Array.from(scrollThead.querySelectorAll('tr'));
    var maxLen = Math.min(fRows.length, sRows.length);

    for (var i = 0; i < maxLen; i++) {
        var fH = fRows[i].getBoundingClientRect().height;
        var sH = sRows[i].getBoundingClientRect().height;
        var maxH = Math.ceil(Math.max(fH, sH));
        if (maxH > 0) {
            fRows[i].style.height = maxH + 'px';
            sRows[i].style.height = maxH + 'px';
            /* 第一行：更新 CSS 变量（供第二行 sticky top 定位） */
            if (i === 0) {
                var val = maxH + 'px';
                var ft = document.getElementById('fina-arturn-frozen-table');
                var st = document.getElementById('fina-arturn-table');
                if (ft) ft.style.setProperty('--fina-arturn-thead-row1-height', val);
                if (st) st.style.setProperty('--fina-arturn-thead-row1-height', val);
            }
        }
    }
}

/**
 * 冻结表列宽由 CSS 变量直接驱动（table-layout:fixed）；
 * 可滚动表测量 thead 第二行各 th 实际宽度写入 colgroup，锁 table-layout:fixed。
 */
function syncFinaArturnColumnWidths() {
    /* 冻结表：CSS 变量已定义宽度，只需锁定 table-layout:fixed */
    var frozenTable = document.getElementById('fina-arturn-frozen-table');
    if (frozenTable) {
        frozenTable.style.tableLayout = 'fixed';
    }
    /* 可滚动表：测量第二行 th 宽度，写入 colgroup */
    var scrollTable = document.getElementById('fina-arturn-table');
    if (!scrollTable) return;
    var colRow = scrollTable.querySelector('thead tr.fina-arturn-thead-cols');
    if (!colRow) return;
    var ths = Array.from(colRow.querySelectorAll('th'));
    if (!ths.length) return;
    var colHtml = ths.map(function (th) {
        var w = Math.ceil(th.getBoundingClientRect().width);
        if (w < 4) w = 48;
        return '<col style="width:' + w + 'px;min-width:' + w + 'px">';
    }).join('');
    var cg = document.getElementById('fina-arturn-colgroup');
    if (cg) cg.innerHTML = colHtml;
    scrollTable.style.tableLayout = 'fixed';
}

/**
 * 分栏辅助初始化（split-pane 架构 2026-04）：
 *  - scrollPane（右）原生 overflow:auto，横向 compositor-thread，零卡顿。
 *  - 纵向 scroll 同步 frozenPane.scrollTop（低频，无 sticky 重算）。
 *  - 行悬停高亮：mouseover/out 同步两面板同行。
 *  - 冻结 thead 的 sort/filter 点击同主 thead 处理。
 *  - Shift+纵轮 → 横向。resize 防抖。
 */
function initFinaArturnHorizScrollHelpers() {
    var scrollPane = document.getElementById('fina-arturn-scroll-pane');
    if (!scrollPane) return;
    if (scrollPane.dataset.finaHorizHelpers === '1') {
        syncFinaArturnTheadStickyHeights();
        return;
    }
    scrollPane.dataset.finaHorizHelpers = '1';

    var frozenPane = document.getElementById('fina-arturn-frozen-pane');
    var frozenTbody = document.getElementById('fina-arturn-frozen-tbody');
    var scrollTbody = document.getElementById('fina-arturn-tbody');

    /* 纵向：同步冻结面板 scrollTop */
    scrollPane.addEventListener('scroll', function () {
        if (frozenPane) frozenPane.scrollTop = scrollPane.scrollTop;
    }, { passive: true });

    /* 行悬停高亮：两面板同行互相同步；
       用 data-row-index（原始行索引）+ trMap 精确查找，不依赖 sectionRowIndex，
       与「DOM 永不销毁、仅 display + 节点重排」的新渲染方案配套 */
    function syncRowHover(srcTbody, ev, isEnter) {
        var tr = ev.target.closest ? ev.target.closest('tr') : null;
        if (!tr || tr.parentElement !== srcTbody) return;
        var origAttr = tr.getAttribute('data-row-index');
        var st = _finaMetricState();
        var fTr = null;
        var sTr = null;
        if (origAttr != null && st._frozenTrMap && st._scrollTrMap) {
            var idxNum = parseInt(origAttr, 10);
            if (idxNum >= 0) {
                fTr = st._frozenTrMap[idxNum] || null;
                sTr = st._scrollTrMap[idxNum] || null;
            }
        }
        if (!fTr && !sTr) {
            /* 兜底：trMap 尚未建立（极早期初始化阶段），按 sectionRowIndex 回退 */
            var idx = tr.sectionRowIndex;
            fTr = frozenTbody && frozenTbody.rows[idx];
            sTr = scrollTbody && scrollTbody.rows[idx];
        }
        if (fTr) fTr.classList.toggle('fina-arturn-row-hover', isEnter);
        if (sTr) sTr.classList.toggle('fina-arturn-row-hover', isEnter);
    }
    if (frozenTbody) {
        frozenTbody.addEventListener('mouseover', function (ev) { syncRowHover(frozenTbody, ev, true); }, { passive: true });
        frozenTbody.addEventListener('mouseout', function (ev) { syncRowHover(frozenTbody, ev, false); }, { passive: true });
    }
    if (scrollTbody) {
        scrollTbody.addEventListener('mouseover', function (ev) { syncRowHover(scrollTbody, ev, true); }, { passive: true });
        scrollTbody.addEventListener('mouseout', function (ev) { syncRowHover(scrollTbody, ev, false); }, { passive: true });
    }

    /* 冻结 thead 的排序/筛选点击 */
    var frozenThead = document.getElementById('fina-arturn-frozen-thead');
    if (frozenThead) {
        frozenThead.addEventListener('click', function (ev) {
            var t = ev.target;
            if (!t || !t.classList) return;
            if (t.classList.contains('fina-sort-btn')) {
                var sk = t.getAttribute('data-sort-key') || '';
                if (sk) { finaMetricCycleSort(sk); renderFinaArturnTable(); saveFinaMetricViewState('arturn'); }
                return;
            }
            if (!t.classList.contains('fina-filter-btn')) return;
            openFinaMetricFilterPopover('arturn', t.getAttribute('data-filter-key') || '', t, ev);
        });
    }

    /* Shift+纵轮 → 横向 */
    scrollPane.addEventListener('wheel', function (ev) {
        if (ev.shiftKey && Math.abs(ev.deltaY) > 0.5) {
            ev.preventDefault();
            scrollPane.scrollLeft = Math.max(0,
                Math.min(scrollPane.scrollWidth - scrollPane.clientWidth, scrollPane.scrollLeft + ev.deltaY));
        }
    }, { passive: false });

    /* resize 防抖 */
    var resizeTimer = null;
    window.addEventListener('resize', function () {
        if (resizeTimer) clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () {
            resizeTimer = null;
            syncFinaArturnTheadStickyHeights();
            syncFinaArturnColumnWidths();
        }, 150);
    });
    syncFinaArturnTheadStickyHeights();
}

function renderFinaMetricTable(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var st = _finaMetricState();
    var thead = document.getElementById(cfg.ids.thead);
    var tbody = document.getElementById(cfg.ids.tbody);
    if (!thead || !tbody) return;
    if (st.sortKey === 'tsCode' || st.sortKey === 'secName') {
        st.sortKey = '';
        st.sortDir = 'asc';
    }
    var periods = Array.isArray(st.periods) ? st.periods : [];
    var y0 = finaArturnPeriodDisplayYear(periods[0]);
    var y1 = finaArturnPeriodDisplayYear(periods[1]);
    var y2 = finaArturnPeriodDisplayYear(periods[2]);
    var y3 = finaArturnPeriodDisplayYear(periods[3]);
    var cps = Array.isArray(st.contractPeriods) ? st.contractPeriods : [];
    var q0 = finaArturnQuarterLabel(cps[0]);
    var q1 = finaArturnQuarterLabel(cps[1]);
    var q2 = finaArturnQuarterLabel(cps[2]);
    var q3 = finaArturnQuarterLabel(cps[3]);
    var q4 = finaArturnQuarterLabel(cps[4]);
    var q5 = finaArturnQuarterLabel(cps[5]);
    var q6 = finaArturnQuarterLabel(cps[6]);
    var q7 = finaArturnQuarterLabel(cps[7]);
    var q8 = finaArturnQuarterLabel(cps[8]);
    /* thead 缓存：只要 periods / contractPeriods 未变，跳过整块 thead.innerHTML 重建
       filter / sort 时通常 periods 不变，省去 185 列 HTML 拼接 + DOM 写入 + 强制 layout */
    var theadKey = periods.join(',') + '|' + cps.join(',');
    var skipTheadRebuild = (theadKey === st._theadKey) &&
        !!document.getElementById('fina-arturn-frozen-thead') &&
        !!document.getElementById(cfg.ids.thead);
    if (!skipTheadRebuild) { st._theadKey = theadKey; }
    if (!skipTheadRebuild) {
    var prevU = '更新：--';
    var elU = document.getElementById(cfg.ids.headUpdated);
    if (elU && elU.textContent) prevU = elU.textContent;
    function buildTh(label, key) {
        var safeLabel = escapeHtmlAttr(label);
        if (key === 'tsCode' || key === 'secName') {
            return '<th><div class="fina-arturn-th-inner">' +
                '<span class="fina-th-label">' + safeLabel + '</span>' +
                '<button type="button" class="fina-filter-btn" data-filter-key="' + key +
                '" title="筛选" aria-label="筛选列 ' + safeLabel + '">▼</button>' +
                '</div></th>';
        }
        var ascOn = st.sortKey === key && st.sortDir === 'asc';
        var descOn = st.sortKey === key && st.sortDir === 'desc';
        var sortCls = 'fina-sort-btn';
        if (ascOn) sortCls += ' is-asc';
        else if (descOn) sortCls += ' is-desc';
        var sortGlyph = ascOn ? '▲' : descOn ? '▼' : '↕';
        var sortTitle = ascOn ? '当前升序，点击改为降序' : descOn ? '当前降序，点击取消排序' : '点击按升序排列';
        var sortAria = ascOn ? safeLabel + ' 升序' : descOn ? safeLabel + ' 降序' : safeLabel + ' 排序（未启用）';
        return '<th><div class="fina-arturn-th-inner">' +
            '<button type="button" class="' + sortCls + '" data-sort-key="' + key + '" title="' +
            escapeHtmlAttr(sortTitle) + '" aria-label="' + sortAria + '">' + sortGlyph + '</button>' +
            '<span class="fina-th-label">' + safeLabel + '</span>' +
            '<button type="button" class="fina-filter-btn" data-filter-key="' + key +
            '" title="筛选" aria-label="筛选列 ' + safeLabel + '">▼</button>' +
            '</div></th>';
    }
    var metricHead =
        '<th colspan="5" class="fina-arturn-metric-cell" scope="colgroup">' +
        '<div class="fina-arturn-metric-cell-inner">' +
        '<button type="button" id="' + cfg.ids.schemeOpen + '" class="fina-arturn-scheme-gear" title="筛选方案" aria-label="打开筛选方案配置">配置</button>' +
        '<span id="' + cfg.ids.headUpdated + '" class="fina-arturn-head-updated">' + _finaArturnEscCellText(prevU) + '</span>' +
        '<span id="' + cfg.ids.msg + '" class="fina-arturn-msg-line"></span>' +
        '</div></th>';
    /* 冻结面板 thead：metric-cell(colspan=5) + 公告日组(colspan=1) + 6 个列头 */
    var frozenThead = document.getElementById('fina-arturn-frozen-thead');
    if (frozenThead) {
        frozenThead.innerHTML =
            '<tr class="fina-arturn-thead-group">' +
            metricHead +
            '<th colspan="1" class="fina-arturn-th-group" scope="colgroup"></th>' +
            '</tr>' +
            '<tr class="fina-arturn-thead-cols">' +
            buildTh('股票代码', 'tsCode') +
            buildTh('股票名称', 'secName') +
            buildTh('申万二级行业', 'swName') +
            buildTh('总市值', 'total_mv_yi') +
            buildTh('自由市值', 'free_mv_yi') +
            buildTh('公告日', 'a0') +
            '</tr>';
    }
    /* 可滚动面板 thead：从上市日开始（原第3格起） */
    thead.innerHTML =
        '<tr class="fina-arturn-thead-group">' +
        '<th colspan="4" class="fina-arturn-th-group" scope="colgroup"></th>' +
        '<th colspan="3" class="fina-arturn-th-group" scope="colgroup">TTM</th>' +
        '<th colspan="4" class="fina-arturn-th-group" scope="colgroup">ROE（扣非）</th>' +
        '<th colspan="4" class="fina-arturn-th-group" scope="colgroup">应收账款周转天数</th>' +
        '<th colspan="4" class="fina-arturn-th-group" scope="colgroup">销售毛利率</th>' +
        '<th colspan="4" class="fina-arturn-th-group" scope="colgroup">经营现金流净额/经营活动净收益</th>' +
        '<th colspan="4" class="fina-arturn-th-group" scope="colgroup">经营现金流净额/营业收入(%)</th>' +
        '<th colspan="10" class="fina-arturn-th-group" scope="colgroup">单季度 营业收入（亿）</th>' +
        '<th colspan="10" class="fina-arturn-th-group" scope="colgroup">单季度 经营现金流净额（亿）</th>' +
        '<th colspan="10" class="fina-arturn-th-group" scope="colgroup">单季度 经营活动净收益（亿）</th>' +
        '<th colspan="10" class="fina-arturn-th-group" scope="colgroup">单季度 扣非净利润（亿）</th>' +
        '<th colspan="10" class="fina-arturn-th-group" scope="colgroup">单季度 FCF（亿）</th>' +
        '<th colspan="11" class="fina-arturn-th-group" scope="colgroup">单季度 经营现金流净额/营业收入(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 经营现金流净额/经营活动净收益(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 营业收入同比增速(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 营业利润同比增长率(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 利润增速高于收入增速(百分点)</th>' +
        '<th colspan="11" class="fina-arturn-th-group" scope="colgroup">单季度 毛利率(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 净利率(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 ROE(扣非)</th>' +
        '<th colspan="12" class="fina-arturn-th-group" scope="colgroup">存货（亿）</th>' +
        '<th colspan="12" class="fina-arturn-th-group" scope="colgroup">合同负债（亿）</th>' +
        '<th colspan="10" class="fina-arturn-th-group" scope="colgroup">固定资产（亿）</th>' +
        '</tr>' +
        '<tr class="fina-arturn-thead-cols">' +
        buildTh('上市日', 'listDate') +
        buildTh('市值/货币', 'money_cap_ratio_pct') +
        buildTh('资产负债率', 'fqm_debt_to_assets') +
        buildTh('带息负债率', 'fqm_interestdebt_ratio') +
        buildTh('营收', 'ttm_revenue_yi') +
        buildTh('PE', 'pe_ttm') +
        buildTh('应收%', 'ar_pct') +
        buildTh(y0, 'roe0') +
        buildTh(y1, 'roe1') +
        buildTh(y2, 'roe2') +
        buildTh(y3, 'roe3') +
        buildTh(y0, 'p0') +
        buildTh(y1, 'p1') +
        buildTh(y2, 'p2') +
        buildTh(y3, 'p3') +
        buildTh(y0, 'g0') +
        buildTh(y1, 'g1') +
        buildTh(y2, 'g2') +
        buildTh(y3, 'g3') +
        buildTh(y0, 'c0') +
        buildTh(y1, 'c1') +
        buildTh(y2, 'c2') +
        buildTh(y3, 'c3') +
        buildTh(y0, 'o0') +
        buildTh(y1, 'o1') +
        buildTh(y2, 'o2') +
        buildTh(y3, 'o3') +
        buildTh('新高', 's_high') +
        buildTh(q0, 's0') +
        buildTh(q1, 's1') +
        buildTh(q2, 's2') +
        buildTh(q3, 's3') +
        buildTh(q4, 's4') +
        buildTh(q5, 's5') +
        buildTh(q6, 's6') +
        buildTh(q7, 's7') +
        buildTh(q8, 's8') +
        buildTh('新高', 'cf_high') +
        buildTh(q0, 'cf0') +
        buildTh(q1, 'cf1') +
        buildTh(q2, 'cf2') +
        buildTh(q3, 'cf3') +
        buildTh(q4, 'cf4') +
        buildTh(q5, 'cf5') +
        buildTh(q6, 'cf6') +
        buildTh(q7, 'cf7') +
        buildTh(q8, 'cf8') +
        buildTh('新高', 'oi_high') +
        buildTh(q0, 'oi0') +
        buildTh(q1, 'oi1') +
        buildTh(q2, 'oi2') +
        buildTh(q3, 'oi3') +
        buildTh(q4, 'oi4') +
        buildTh(q5, 'oi5') +
        buildTh(q6, 'oi6') +
        buildTh(q7, 'oi7') +
        buildTh(q8, 'oi8') +
        buildTh('新高', 'dtp_high') +
        buildTh(q0, 'dtp0') +
        buildTh(q1, 'dtp1') +
        buildTh(q2, 'dtp2') +
        buildTh(q3, 'dtp3') +
        buildTh(q4, 'dtp4') +
        buildTh(q5, 'dtp5') +
        buildTh(q6, 'dtp6') +
        buildTh(q7, 'dtp7') +
        buildTh(q8, 'dtp8') +
        buildTh('新高', 'fst_high') +
        buildTh(q0, 'fst0') +
        buildTh(q1, 'fst1') +
        buildTh(q2, 'fst2') +
        buildTh(q3, 'fst3') +
        buildTh(q4, 'fst4') +
        buildTh(q5, 'fst5') +
        buildTh(q6, 'fst6') +
        buildTh(q7, 'fst7') +
        buildTh(q8, 'fst8') +
        buildTh('新高', 'ocs_high') +
        buildTh('加权CV', 'ocs_wqcv') +
        buildTh(q0, 'ocs0') +
        buildTh(q1, 'ocs1') +
        buildTh(q2, 'ocs2') +
        buildTh(q3, 'ocs3') +
        buildTh(q4, 'ocs4') +
        buildTh(q5, 'ocs5') +
        buildTh(q6, 'ocs6') +
        buildTh(q7, 'ocs7') +
        buildTh(q8, 'ocs8') +
        buildTh(q0, 'oco0') +
        buildTh(q1, 'oco1') +
        buildTh(q2, 'oco2') +
        buildTh(q3, 'oco3') +
        buildTh(q4, 'oco4') +
        buildTh(q5, 'oco5') +
        buildTh(q6, 'oco6') +
        buildTh(q7, 'oco7') +
        buildTh(q8, 'oco8') +
        buildTh(q0, 'r0') +
        buildTh(q1, 'r1') +
        buildTh(q2, 'r2') +
        buildTh(q3, 'r3') +
        buildTh(q4, 'r4') +
        buildTh(q5, 'r5') +
        buildTh(q6, 'r6') +
        buildTh(q7, 'r7') +
        buildTh(q8, 'r8') +
        buildTh(q0, 'u0') +
        buildTh(q1, 'u1') +
        buildTh(q2, 'u2') +
        buildTh(q3, 'u3') +
        buildTh(q4, 'u4') +
        buildTh(q5, 'u5') +
        buildTh(q6, 'u6') +
        buildTh(q7, 'u7') +
        buildTh(q8, 'u8') +
        buildTh(q0, 'v0') +
        buildTh(q1, 'v1') +
        buildTh(q2, 'v2') +
        buildTh(q3, 'v3') +
        buildTh(q4, 'v4') +
        buildTh(q5, 'v5') +
        buildTh(q6, 'v6') +
        buildTh(q7, 'v7') +
        buildTh(q8, 'v8') +
        buildTh('新高', 'm_high') +
        buildTh('环比', 'm_qoq') +
        buildTh(q0, 'm0') +
        buildTh(q1, 'm1') +
        buildTh(q2, 'm2') +
        buildTh(q3, 'm3') +
        buildTh(q4, 'm4') +
        buildTh(q5, 'm5') +
        buildTh(q6, 'm6') +
        buildTh(q7, 'm7') +
        buildTh(q8, 'm8') +
        buildTh(q0, 'n0') +
        buildTh(q1, 'n1') +
        buildTh(q2, 'n2') +
        buildTh(q3, 'n3') +
        buildTh(q4, 'n4') +
        buildTh(q5, 'n5') +
        buildTh(q6, 'n6') +
        buildTh(q7, 'n7') +
        buildTh(q8, 'n8') +
        buildTh(q0, 't0') +
        buildTh(q1, 't1') +
        buildTh(q2, 't2') +
        buildTh(q3, 't3') +
        buildTh(q4, 't4') +
        buildTh(q5, 't5') +
        buildTh(q6, 't6') +
        buildTh(q7, 't7') +
        buildTh(q8, 't8') +
        buildTh('Chg%', 'invchg') +
        buildTh('营收%', 'inv_revenue_pct') +
        buildTh('新高', 'inv_high') +
        buildTh(q0, 'inv0') +
        buildTh(q1, 'inv1') +
        buildTh(q2, 'inv2') +
        buildTh(q3, 'inv3') +
        buildTh(q4, 'inv4') +
        buildTh(q5, 'inv5') +
        buildTh(q6, 'inv6') +
        buildTh(q7, 'inv7') +
        buildTh(q8, 'inv8') +
        buildTh('Chg%', 'kchg') +
        buildTh('营收%', 'k_revenue_pct') +
        buildTh('新高', 'k_high') +
        buildTh(q0, 'k0') +
        buildTh(q1, 'k1') +
        buildTh(q2, 'k2') +
        buildTh(q3, 'k3') +
        buildTh(q4, 'k4') +
        buildTh(q5, 'k5') +
        buildTh(q6, 'k6') +
        buildTh(q7, 'k7') +
        buildTh(q8, 'k8') +
        buildTh('Chg%', 'fchg') +
        buildTh(q0, 'f0') +
        buildTh(q1, 'f1') +
        buildTh(q2, 'f2') +
        buildTh(q3, 'f3') +
        buildTh(q4, 'f4') +
        buildTh(q5, 'f5') +
        buildTh(q6, 'f6') +
        buildTh(q7, 'f7') +
        buildTh(q8, 'f8') +
        '</tr>';
    } else {
        /* thead 内容未变（periods/contractPeriods 一致），仅原地更新排序按钮 class / 字符 */
        ['fina-arturn-thead', 'fina-arturn-frozen-thead'].forEach(function (theadId) {
            var tel = document.getElementById(theadId);
            if (!tel) return;
            tel.querySelectorAll('.fina-sort-btn').forEach(function (btn) {
                var k = btn.getAttribute('data-sort-key') || '';
                var ascOn = k === st.sortKey && st.sortDir === 'asc';
                var descOn = k === st.sortKey && st.sortDir === 'desc';
                btn.classList.toggle('is-asc', ascOn);
                btn.classList.toggle('is-desc', descOn);
                btn.textContent = ascOn ? '▲' : descOn ? '▼' : '↕';
                btn.setAttribute('title', ascOn ? '当前升序，点击改为降序' :
                    descOn ? '当前降序，点击取消排序' : '点击按升序排列');
            });
        });
    }
    hideFinaBootHint();

    var filters = getFinaMetricFilters(panelKey);
    var codeKw = filters.tsCode.toLowerCase();
    var nameKw = filters.secName.toLowerCase();
    var swOrKws = finaArturnSplitSwNameOrKeywords(filters.swName);
    var ma0 = String(filters.a0 || '').trim().toLowerCase();
    var listDateKw = String(filters.listDate || '').trim().toLowerCase();
    var mTotalMv = buildNumericFilterMatcher(filters.total_mv_yi);
    var mFreeMv = buildNumericFilterMatcher(filters.free_mv_yi);
    var mMoneyCapRatio = buildNumericFilterMatcher(filters.money_cap_ratio_pct);
    var mFqmDebtToAssets = buildNumericFilterMatcher(filters.fqm_debt_to_assets);
    var mFqmInterestdebtRatio = buildNumericFilterMatcher(filters.fqm_interestdebt_ratio);
    var mttmYi = buildNumericFilterMatcher(filters.ttm_revenue_yi);
    var mPe = buildNumericFilterMatcher(filters.pe_ttm);
    var mar_pct = buildNumericFilterMatcher(filters.ar_pct);
    var mr0 = buildNumericFilterMatcher(filters.roe0);
    var mr1 = buildNumericFilterMatcher(filters.roe1);
    var mr2 = buildNumericFilterMatcher(filters.roe2);
    var mr3 = buildNumericFilterMatcher(filters.roe3);
    var m0 = buildNumericFilterMatcher(filters.p0);
    var m1 = buildNumericFilterMatcher(filters.p1);
    var m2 = buildNumericFilterMatcher(filters.p2);
    var m3 = buildNumericFilterMatcher(filters.p3);
    var mg0 = buildNumericFilterMatcher(filters.g0);
    var mg1 = buildNumericFilterMatcher(filters.g1);
    var mg2 = buildNumericFilterMatcher(filters.g2);
    var mg3 = buildNumericFilterMatcher(filters.g3);
    var mc0 = buildNumericFilterMatcher(filters.c0);
    var mc1 = buildNumericFilterMatcher(filters.c1);
    var mc2 = buildNumericFilterMatcher(filters.c2);
    var mc3 = buildNumericFilterMatcher(filters.c3);
    var mo0 = buildNumericFilterMatcher(filters.o0);
    var mo1 = buildNumericFilterMatcher(filters.o1);
    var mo2 = buildNumericFilterMatcher(filters.o2);
    var mo3 = buildNumericFilterMatcher(filters.o3);
    var ms_high = buildNumericFilterMatcher(filters.s_high);
    var ms0 = buildNumericFilterMatcher(filters.s0);
    var ms1 = buildNumericFilterMatcher(filters.s1);
    var ms2 = buildNumericFilterMatcher(filters.s2);
    var ms3 = buildNumericFilterMatcher(filters.s3);
    var ms4 = buildNumericFilterMatcher(filters.s4);
    var ms5 = buildNumericFilterMatcher(filters.s5);
    var ms6 = buildNumericFilterMatcher(filters.s6);
    var ms7 = buildNumericFilterMatcher(filters.s7);
    var ms8 = buildNumericFilterMatcher(filters.s8);
    var mcf_high = buildNumericFilterMatcher(filters.cf_high);
    var mcf0 = buildNumericFilterMatcher(filters.cf0);
    var mcf1 = buildNumericFilterMatcher(filters.cf1);
    var mcf2 = buildNumericFilterMatcher(filters.cf2);
    var mcf3 = buildNumericFilterMatcher(filters.cf3);
    var mcf4 = buildNumericFilterMatcher(filters.cf4);
    var mcf5 = buildNumericFilterMatcher(filters.cf5);
    var mcf6 = buildNumericFilterMatcher(filters.cf6);
    var mcf7 = buildNumericFilterMatcher(filters.cf7);
    var mcf8 = buildNumericFilterMatcher(filters.cf8);
    var moi_high = buildNumericFilterMatcher(filters.oi_high);
    var moi0 = buildNumericFilterMatcher(filters.oi0);
    var moi1 = buildNumericFilterMatcher(filters.oi1);
    var moi2 = buildNumericFilterMatcher(filters.oi2);
    var moi3 = buildNumericFilterMatcher(filters.oi3);
    var moi4 = buildNumericFilterMatcher(filters.oi4);
    var moi5 = buildNumericFilterMatcher(filters.oi5);
    var moi6 = buildNumericFilterMatcher(filters.oi6);
    var moi7 = buildNumericFilterMatcher(filters.oi7);
    var moi8 = buildNumericFilterMatcher(filters.oi8);
    var mdtp_high = buildNumericFilterMatcher(filters.dtp_high);
    var mocs_high = buildNumericFilterMatcher(filters.ocs_high);
    var mocs_wqcv = buildNumericFilterMatcher(filters.ocs_wqcv);
    var mdtp0 = buildNumericFilterMatcher(filters.dtp0);
    var mdtp1 = buildNumericFilterMatcher(filters.dtp1);
    var mdtp2 = buildNumericFilterMatcher(filters.dtp2);
    var mdtp3 = buildNumericFilterMatcher(filters.dtp3);
    var mdtp4 = buildNumericFilterMatcher(filters.dtp4);
    var mdtp5 = buildNumericFilterMatcher(filters.dtp5);
    var mdtp6 = buildNumericFilterMatcher(filters.dtp6);
    var mdtp7 = buildNumericFilterMatcher(filters.dtp7);
    var mdtp8 = buildNumericFilterMatcher(filters.dtp8);
    var mfst_high = buildNumericFilterMatcher(filters.fst_high);
    var mfst0 = buildNumericFilterMatcher(filters.fst0);
    var mfst1 = buildNumericFilterMatcher(filters.fst1);
    var mfst2 = buildNumericFilterMatcher(filters.fst2);
    var mfst3 = buildNumericFilterMatcher(filters.fst3);
    var mfst4 = buildNumericFilterMatcher(filters.fst4);
    var mfst5 = buildNumericFilterMatcher(filters.fst5);
    var mfst6 = buildNumericFilterMatcher(filters.fst6);
    var mfst7 = buildNumericFilterMatcher(filters.fst7);
    var mfst8 = buildNumericFilterMatcher(filters.fst8);
    var mocs0 = buildNumericFilterMatcher(filters.ocs0);
    var mocs1 = buildNumericFilterMatcher(filters.ocs1);
    var mocs2 = buildNumericFilterMatcher(filters.ocs2);
    var mocs3 = buildNumericFilterMatcher(filters.ocs3);
    var mocs4 = buildNumericFilterMatcher(filters.ocs4);
    var mocs5 = buildNumericFilterMatcher(filters.ocs5);
    var mocs6 = buildNumericFilterMatcher(filters.ocs6);
    var mocs7 = buildNumericFilterMatcher(filters.ocs7);
    var mocs8 = buildNumericFilterMatcher(filters.ocs8);
    var moco0 = buildNumericFilterMatcher(filters.oco0);
    var moco1 = buildNumericFilterMatcher(filters.oco1);
    var moco2 = buildNumericFilterMatcher(filters.oco2);
    var moco3 = buildNumericFilterMatcher(filters.oco3);
    var moco4 = buildNumericFilterMatcher(filters.oco4);
    var moco5 = buildNumericFilterMatcher(filters.oco5);
    var moco6 = buildNumericFilterMatcher(filters.oco6);
    var moco7 = buildNumericFilterMatcher(filters.oco7);
    var moco8 = buildNumericFilterMatcher(filters.oco8);
    var mrq0 = buildNumericFilterMatcher(filters.r0);
    var mrq1 = buildNumericFilterMatcher(filters.r1);
    var mrq2 = buildNumericFilterMatcher(filters.r2);
    var mrq3 = buildNumericFilterMatcher(filters.r3);
    var mrq4 = buildNumericFilterMatcher(filters.r4);
    var mrq5 = buildNumericFilterMatcher(filters.r5);
    var mrq6 = buildNumericFilterMatcher(filters.r6);
    var mrq7 = buildNumericFilterMatcher(filters.r7);
    var mrq8 = buildNumericFilterMatcher(filters.r8);
    var mk0 = buildNumericFilterMatcher(filters.k0);
    var mk1 = buildNumericFilterMatcher(filters.k1);
    var mk2 = buildNumericFilterMatcher(filters.k2);
    var mk3 = buildNumericFilterMatcher(filters.k3);
    var mk4 = buildNumericFilterMatcher(filters.k4);
    var mk5 = buildNumericFilterMatcher(filters.k5);
    var mk6 = buildNumericFilterMatcher(filters.k6);
    var mk7 = buildNumericFilterMatcher(filters.k7);
    var mk8 = buildNumericFilterMatcher(filters.k8);
    var mkchg = buildNumericFilterMatcher(filters.kchg);
    var mkRevPct = buildNumericFilterMatcher(filters.k_revenue_pct);
    var mk_high = buildNumericFilterMatcher(filters.k_high);
    var mf0 = buildNumericFilterMatcher(filters.f0);
    var mf1 = buildNumericFilterMatcher(filters.f1);
    var mf2 = buildNumericFilterMatcher(filters.f2);
    var mf3 = buildNumericFilterMatcher(filters.f3);
    var mf4 = buildNumericFilterMatcher(filters.f4);
    var mf5 = buildNumericFilterMatcher(filters.f5);
    var mf6 = buildNumericFilterMatcher(filters.f6);
    var mf7 = buildNumericFilterMatcher(filters.f7);
    var mf8 = buildNumericFilterMatcher(filters.f8);
    var mfchg = buildNumericFilterMatcher(filters.fchg);
    var mu0 = buildNumericFilterMatcher(filters.u0);
    var mu1 = buildNumericFilterMatcher(filters.u1);
    var mu2 = buildNumericFilterMatcher(filters.u2);
    var mu3 = buildNumericFilterMatcher(filters.u3);
    var mu4 = buildNumericFilterMatcher(filters.u4);
    var mu5 = buildNumericFilterMatcher(filters.u5);
    var mu6 = buildNumericFilterMatcher(filters.u6);
    var mu7 = buildNumericFilterMatcher(filters.u7);
    var mu8 = buildNumericFilterMatcher(filters.u8);
    var mv0 = buildNumericFilterMatcher(filters.v0);
    var mv1 = buildNumericFilterMatcher(filters.v1);
    var mv2 = buildNumericFilterMatcher(filters.v2);
    var mv3 = buildNumericFilterMatcher(filters.v3);
    var mv4 = buildNumericFilterMatcher(filters.v4);
    var mv5 = buildNumericFilterMatcher(filters.v5);
    var mv6 = buildNumericFilterMatcher(filters.v6);
    var mv7 = buildNumericFilterMatcher(filters.v7);
    var mv8 = buildNumericFilterMatcher(filters.v8);
    var mmHigh = buildNumericFilterMatcher(filters.m_high);
    var mmQoq = buildNumericFilterMatcher(filters.m_qoq);
    var mm0 = buildNumericFilterMatcher(filters.m0);
    var mm1 = buildNumericFilterMatcher(filters.m1);
    var mm2 = buildNumericFilterMatcher(filters.m2);
    var mm3 = buildNumericFilterMatcher(filters.m3);
    var mm4 = buildNumericFilterMatcher(filters.m4);
    var mm5 = buildNumericFilterMatcher(filters.m5);
    var mm6 = buildNumericFilterMatcher(filters.m6);
    var mm7 = buildNumericFilterMatcher(filters.m7);
    var mm8 = buildNumericFilterMatcher(filters.m8);
    var mn0 = buildNumericFilterMatcher(filters.n0);
    var mn1 = buildNumericFilterMatcher(filters.n1);
    var mn2 = buildNumericFilterMatcher(filters.n2);
    var mn3 = buildNumericFilterMatcher(filters.n3);
    var mn4 = buildNumericFilterMatcher(filters.n4);
    var mn5 = buildNumericFilterMatcher(filters.n5);
    var mn6 = buildNumericFilterMatcher(filters.n6);
    var mn7 = buildNumericFilterMatcher(filters.n7);
    var mn8 = buildNumericFilterMatcher(filters.n8);
    var mt0 = buildNumericFilterMatcher(filters.t0);
    var mt1 = buildNumericFilterMatcher(filters.t1);
    var mt2 = buildNumericFilterMatcher(filters.t2);
    var mt3 = buildNumericFilterMatcher(filters.t3);
    var mt4 = buildNumericFilterMatcher(filters.t4);
    var mt5 = buildNumericFilterMatcher(filters.t5);
    var mt6 = buildNumericFilterMatcher(filters.t6);
    var mt7 = buildNumericFilterMatcher(filters.t7);
    var mt8 = buildNumericFilterMatcher(filters.t8);
    var minv0 = buildNumericFilterMatcher(filters.inv0);
    var minv1 = buildNumericFilterMatcher(filters.inv1);
    var minv2 = buildNumericFilterMatcher(filters.inv2);
    var minv3 = buildNumericFilterMatcher(filters.inv3);
    var minv4 = buildNumericFilterMatcher(filters.inv4);
    var minv5 = buildNumericFilterMatcher(filters.inv5);
    var minv6 = buildNumericFilterMatcher(filters.inv6);
    var minv7 = buildNumericFilterMatcher(filters.inv7);
    var minv8 = buildNumericFilterMatcher(filters.inv8);
    var minvchg = buildNumericFilterMatcher(filters.invchg);
    var minvRevPct = buildNumericFilterMatcher(filters.inv_revenue_pct);
    var minv_high = buildNumericFilterMatcher(filters.inv_high);

    var rows = Array.isArray(st.rows) ? st.rows : [];
    var filtered = rows.filter(function (row) {
        var tsCode = String(row.ts_code || '');
        var secName = String(row.sec_name || '');
        var swName = String(row.sw_name || '');
        var a0 = String(row.a0 || '');
        var listDate = String(row.list_date || '');
        if (st.excludeBj && /\.BJ$/i.test(tsCode.trim())) return false;
        if (st.excludeSt && finaArturnIsStStockName(secName)) return false;
        if (codeKw && tsCode.toLowerCase().indexOf(codeKw) < 0) return false;
        if (nameKw && secName.toLowerCase().indexOf(nameKw) < 0) return false;
        if (swOrKws.length) {
            var swHay = swName.toLowerCase();
            var swHit = swOrKws.some(function (k) {
                return swHay.indexOf(String(k).toLowerCase()) >= 0;
            });
            if (!swHit) return false;
        }
        if (mTotalMv && !mTotalMv(row.total_mv_yi)) return false;
        if (mFreeMv && !mFreeMv(row.free_mv_yi)) return false;
        if (ma0 && !finaArturnDateTextMatch(a0, ma0)) return false;
        if (listDateKw && !finaArturnDateTextMatch(listDate, listDateKw)) return false;
        if (mMoneyCapRatio && !mMoneyCapRatio(row.money_cap_ratio_pct)) return false;
        if (mFqmDebtToAssets && !mFqmDebtToAssets(row.fqm_debt_to_assets)) return false;
        if (mFqmInterestdebtRatio && !mFqmInterestdebtRatio(row.fqm_interestdebt_ratio)) return false;
        if (mttmYi && !mttmYi(row.ttm_revenue_yi)) return false;
        if (mPe && !mPe(row.pe_ttm)) return false;
        if (mar_pct && !mar_pct(row.ar_pct)) return false;
        if (mr0 && !mr0(row.roe0)) return false;
        if (mr1 && !mr1(row.roe1)) return false;
        if (mr2 && !mr2(row.roe2)) return false;
        if (mr3 && !mr3(row.roe3)) return false;
        if (m0 && !m0(row.p0)) return false;
        if (m1 && !m1(row.p1)) return false;
        if (m2 && !m2(row.p2)) return false;
        if (m3 && !m3(row.p3)) return false;
        if (mg0 && !mg0(row.g0)) return false;
        if (mg1 && !mg1(row.g1)) return false;
        if (mg2 && !mg2(row.g2)) return false;
        if (mg3 && !mg3(row.g3)) return false;
        if (mc0 && !mc0(row.c0)) return false;
        if (mc1 && !mc1(row.c1)) return false;
        if (mc2 && !mc2(row.c2)) return false;
        if (mc3 && !mc3(row.c3)) return false;
        if (mo0 && !mo0(row.o0)) return false;
        if (mo1 && !mo1(row.o1)) return false;
        if (mo2 && !mo2(row.o2)) return false;
        if (mo3 && !mo3(row.o3)) return false;
        if (ms_high && !ms_high(row.s_high)) return false;
        if (ms0 && !ms0(row.s0)) return false;
        if (ms1 && !ms1(row.s1)) return false;
        if (ms2 && !ms2(row.s2)) return false;
        if (ms3 && !ms3(row.s3)) return false;
        if (ms4 && !ms4(row.s4)) return false;
        if (ms5 && !ms5(row.s5)) return false;
        if (ms6 && !ms6(row.s6)) return false;
        if (ms7 && !ms7(row.s7)) return false;
        if (ms8 && !ms8(row.s8)) return false;
        if (mcf_high && !mcf_high(row.cf_high)) return false;
        if (mcf0 && !mcf0(row.cf0)) return false;
        if (mcf1 && !mcf1(row.cf1)) return false;
        if (mcf2 && !mcf2(row.cf2)) return false;
        if (mcf3 && !mcf3(row.cf3)) return false;
        if (mcf4 && !mcf4(row.cf4)) return false;
        if (mcf5 && !mcf5(row.cf5)) return false;
        if (mcf6 && !mcf6(row.cf6)) return false;
        if (mcf7 && !mcf7(row.cf7)) return false;
        if (mcf8 && !mcf8(row.cf8)) return false;
        if (moi_high && !moi_high(row.oi_high)) return false;
        if (moi0 && !moi0(row.oi0)) return false;
        if (moi1 && !moi1(row.oi1)) return false;
        if (moi2 && !moi2(row.oi2)) return false;
        if (moi3 && !moi3(row.oi3)) return false;
        if (moi4 && !moi4(row.oi4)) return false;
        if (moi5 && !moi5(row.oi5)) return false;
        if (moi6 && !moi6(row.oi6)) return false;
        if (moi7 && !moi7(row.oi7)) return false;
        if (moi8 && !moi8(row.oi8)) return false;
        if (mdtp_high && !mdtp_high(row.dtp_high)) return false;
        if (mocs_high && !mocs_high(row.ocs_high)) return false;
        if (mocs_wqcv && !mocs_wqcv(row.ocs_wqcv)) return false;
        if (mdtp0 && !mdtp0(row.dtp0)) return false;
        if (mdtp1 && !mdtp1(row.dtp1)) return false;
        if (mdtp2 && !mdtp2(row.dtp2)) return false;
        if (mdtp3 && !mdtp3(row.dtp3)) return false;
        if (mdtp4 && !mdtp4(row.dtp4)) return false;
        if (mdtp5 && !mdtp5(row.dtp5)) return false;
        if (mdtp6 && !mdtp6(row.dtp6)) return false;
        if (mdtp7 && !mdtp7(row.dtp7)) return false;
        if (mdtp8 && !mdtp8(row.dtp8)) return false;
        if (mfst_high && !mfst_high(row.fst_high)) return false;
        if (mfst0 && !mfst0(row.fst0)) return false;
        if (mfst1 && !mfst1(row.fst1)) return false;
        if (mfst2 && !mfst2(row.fst2)) return false;
        if (mfst3 && !mfst3(row.fst3)) return false;
        if (mfst4 && !mfst4(row.fst4)) return false;
        if (mfst5 && !mfst5(row.fst5)) return false;
        if (mfst6 && !mfst6(row.fst6)) return false;
        if (mfst7 && !mfst7(row.fst7)) return false;
        if (mfst8 && !mfst8(row.fst8)) return false;
        if (mocs0 && !mocs0(row.ocs0)) return false;
        if (mocs1 && !mocs1(row.ocs1)) return false;
        if (mocs2 && !mocs2(row.ocs2)) return false;
        if (mocs3 && !mocs3(row.ocs3)) return false;
        if (mocs4 && !mocs4(row.ocs4)) return false;
        if (mocs5 && !mocs5(row.ocs5)) return false;
        if (mocs6 && !mocs6(row.ocs6)) return false;
        if (mocs7 && !mocs7(row.ocs7)) return false;
        if (mocs8 && !mocs8(row.ocs8)) return false;
        if (moco0 && !moco0(row.oco0)) return false;
        if (moco1 && !moco1(row.oco1)) return false;
        if (moco2 && !moco2(row.oco2)) return false;
        if (moco3 && !moco3(row.oco3)) return false;
        if (moco4 && !moco4(row.oco4)) return false;
        if (moco5 && !moco5(row.oco5)) return false;
        if (moco6 && !moco6(row.oco6)) return false;
        if (moco7 && !moco7(row.oco7)) return false;
        if (moco8 && !moco8(row.oco8)) return false;
        if (mrq0 && !mrq0(row.r0)) return false;
        if (mrq1 && !mrq1(row.r1)) return false;
        if (mrq2 && !mrq2(row.r2)) return false;
        if (mrq3 && !mrq3(row.r3)) return false;
        if (mrq4 && !mrq4(row.r4)) return false;
        if (mrq5 && !mrq5(row.r5)) return false;
        if (mrq6 && !mrq6(row.r6)) return false;
        if (mrq7 && !mrq7(row.r7)) return false;
        if (mrq8 && !mrq8(row.r8)) return false;
        if (mk0 && !mk0(row.k0)) return false;
        if (mk1 && !mk1(row.k1)) return false;
        if (mk2 && !mk2(row.k2)) return false;
        if (mk3 && !mk3(row.k3)) return false;
        if (mk4 && !mk4(row.k4)) return false;
        if (mk5 && !mk5(row.k5)) return false;
        if (mk6 && !mk6(row.k6)) return false;
        if (mk7 && !mk7(row.k7)) return false;
        if (mk8 && !mk8(row.k8)) return false;
        if (mf0 && !mf0(row.f0)) return false;
        if (mf1 && !mf1(row.f1)) return false;
        if (mf2 && !mf2(row.f2)) return false;
        if (mf3 && !mf3(row.f3)) return false;
        if (mf4 && !mf4(row.f4)) return false;
        if (mf5 && !mf5(row.f5)) return false;
        if (mf6 && !mf6(row.f6)) return false;
        if (mf7 && !mf7(row.f7)) return false;
        if (mf8 && !mf8(row.f8)) return false;
        if (mu0 && !mu0(row.u0)) return false;
        if (mu1 && !mu1(row.u1)) return false;
        if (mu2 && !mu2(row.u2)) return false;
        if (mu3 && !mu3(row.u3)) return false;
        if (mu4 && !mu4(row.u4)) return false;
        if (mu5 && !mu5(row.u5)) return false;
        if (mu6 && !mu6(row.u6)) return false;
        if (mu7 && !mu7(row.u7)) return false;
        if (mu8 && !mu8(row.u8)) return false;
        if (mv0 && !mv0(row.v0)) return false;
        if (mv1 && !mv1(row.v1)) return false;
        if (mv2 && !mv2(row.v2)) return false;
        if (mv3 && !mv3(row.v3)) return false;
        if (mv4 && !mv4(row.v4)) return false;
        if (mv5 && !mv5(row.v5)) return false;
        if (mv6 && !mv6(row.v6)) return false;
        if (mv7 && !mv7(row.v7)) return false;
        if (mv8 && !mv8(row.v8)) return false;
        if (mmHigh && !mmHigh(row.m_high)) return false;
        if (mmQoq && !mmQoq(row.m_qoq)) return false;
        if (mm0 && !mm0(row.m0)) return false;
        if (mm1 && !mm1(row.m1)) return false;
        if (mm2 && !mm2(row.m2)) return false;
        if (mm3 && !mm3(row.m3)) return false;
        if (mm4 && !mm4(row.m4)) return false;
        if (mm5 && !mm5(row.m5)) return false;
        if (mm6 && !mm6(row.m6)) return false;
        if (mm7 && !mm7(row.m7)) return false;
        if (mm8 && !mm8(row.m8)) return false;
        if (mn0 && !mn0(row.n0)) return false;
        if (mn1 && !mn1(row.n1)) return false;
        if (mn2 && !mn2(row.n2)) return false;
        if (mn3 && !mn3(row.n3)) return false;
        if (mn4 && !mn4(row.n4)) return false;
        if (mn5 && !mn5(row.n5)) return false;
        if (mn6 && !mn6(row.n6)) return false;
        if (mn7 && !mn7(row.n7)) return false;
        if (mn8 && !mn8(row.n8)) return false;
        if (mt0 && !mt0(row.t0)) return false;
        if (mt1 && !mt1(row.t1)) return false;
        if (mt2 && !mt2(row.t2)) return false;
        if (mt3 && !mt3(row.t3)) return false;
        if (mt4 && !mt4(row.t4)) return false;
        if (mt5 && !mt5(row.t5)) return false;
        if (mt6 && !mt6(row.t6)) return false;
        if (mt7 && !mt7(row.t7)) return false;
        if (mt8 && !mt8(row.t8)) return false;
        if (minv0 && !minv0(row.inv0)) return false;
        if (minv1 && !minv1(row.inv1)) return false;
        if (minv2 && !minv2(row.inv2)) return false;
        if (minv3 && !minv3(row.inv3)) return false;
        if (minv4 && !minv4(row.inv4)) return false;
        if (minv5 && !minv5(row.inv5)) return false;
        if (minv6 && !minv6(row.inv6)) return false;
        if (minv7 && !minv7(row.inv7)) return false;
        if (minv8 && !minv8(row.inv8)) return false;
        if (minvchg && !minvchg(computeInventoryChg(row))) return false;
        if (minvRevPct && !minvRevPct(row.inv_revenue_pct)) return false;
        if (minv_high && !minv_high(row.inv_high)) return false;
        if (mkchg && !mkchg(computeContractLiabChg(row))) return false;
        if (mkRevPct && !mkRevPct(row.k_revenue_pct)) return false;
        if (mk_high && !mk_high(row.k_high)) return false;
        if (mfchg && !mfchg(computeFixedAssetsChg(row))) return false;
        return true;
    });

    var sk = String(st.sortKey || '');
    var sd = st.sortDir === 'desc' ? 'desc' : 'asc';
    if (sk && finaArturnIsSortableColumnKey(sk)) {
        filtered = filtered.slice().sort(function (a, b) {
            return finaArturnCompareRows(a, b, sk, sd);
        });
    }
    st.fixedAssetsNavList = filtered.map(function (row) {
        return { ts_code: String(row.ts_code || ''), sec_name: String(row.sec_name || '') };
    });

    if (!filtered.length) {
        if (st._renderChunkRaf) {
            cancelAnimationFrame(st._renderChunkRaf);
            st._renderChunkRaf = 0;
        }
        st._renderToken = (st._renderToken || 0) + 1;
        var _ftb = document.getElementById('fina-arturn-frozen-tbody');
        if (_ftb) _ftb.innerHTML = '<tr><td colspan="6" class="median-table-cat">无匹配数据</td></tr>';
        tbody.innerHTML = '<tr><td colspan="187" class="median-table-cat">无匹配数据</td></tr>';
        /* 占位已写入 tbody，trMap 已失效；下次非空时必须走全量重建 */
        st._domBuiltRowsVersion = -1;
        st._frozenTrMap = null;
        st._scrollTrMap = null;
    } else {
        /* 终极方案：DOM 一次建好、永不销毁
           - 首次按 st.rows 原始顺序全量 build（data-row-index=原始索引）
           - 筛选：只切换 display + DocumentFragment 重排，不销毁任何节点
           - 排序：同样是节点重排（appendChild = move）
           fetch 新数据时 _rowsVersion++ → 触发下次全量重建 */
        var buildRowHtml = function (row, origIdx, zebraPos) {
            var trClass = (zebraPos % 2 === 1) ? ' median-table-zebra' : '';
            var swRaw = row.sw_name != null ? String(row.sw_name) : '';
            var swTitle = swRaw ? ' title="' + escapeHtmlAttr(swRaw) + '"' : '';
            var trOpen = '<tr class="median-table-row' + trClass + '" data-row-index="' + origIdx + '">';
            /* 冻结面板：前 6 列（股票代码/名称/申万/总市值/自由市值/公告日） */
            var fHtml = trOpen +
                '<td class="median-table-cat">' + escapeHtmlAttr(row.ts_code || '--') + '</td>' +
                '<td class="median-table-cat"><span class="fina-fixed-assets-clickable" data-fixed-assets-ts-code="' + escapeHtmlAttr(row.ts_code || '') + '" data-fixed-assets-sec-name="' + escapeHtmlAttr(row.sec_name || '') + '" title="查看固定资产、在建工程与营收（最多25季）">' + escapeHtmlAttr(row.sec_name || '--') + '</span></td>' +
                '<td class="median-table-cat fina-arturn-sw-cell"' + swTitle + '>' + escapeHtmlAttr(swRaw || '--') + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.total_mv_yi, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.free_mv_yi, 1) + '</td>' +
                '<td class="median-table-num">' + escapeHtmlAttr(formatFinaArturnAnnDate(row.a0)) + '</td>' +
                '</tr>';
            /* 可滚动面板：第 7 列起（上市日…固定资产） */
            var sHtml = trOpen +
                '<td class="median-table-num">' + escapeHtmlAttr(formatFinaArturnAnnDate(row.list_date)) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.money_cap_ratio_pct, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.fqm_debt_to_assets, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.fqm_interestdebt_ratio, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.ttm_revenue_yi, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.pe_ttm, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.ar_pct, 1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.roe0) + '">' + formatFinaArturnValue(row.roe0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.roe1) + '">' + formatFinaArturnValue(row.roe1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.roe2) + '">' + formatFinaArturnValue(row.roe2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.roe3) + '">' + formatFinaArturnValue(row.roe3) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.p0, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.p1, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.p2, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.p3, 1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.g0) + '">' + formatFinaArturnValue(row.g0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.g1) + '">' + formatFinaArturnValue(row.g1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.g2) + '">' + formatFinaArturnValue(row.g2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.g3) + '">' + formatFinaArturnValue(row.g3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.c0) + '">' + formatFinaArturnValue(row.c0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.c1) + '">' + formatFinaArturnValue(row.c1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.c2) + '">' + formatFinaArturnValue(row.c2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.c3) + '">' + formatFinaArturnValue(row.c3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.o0) + '">' + formatFinaArturnOcfOrPct(row.o0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.o1) + '">' + formatFinaArturnOcfOrPct(row.o1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.o2) + '">' + formatFinaArturnOcfOrPct(row.o2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.o3) + '">' + formatFinaArturnOcfOrPct(row.o3) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s_high, 0) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s0) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s3) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s4) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s5) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s6) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s7) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.s8) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.cf_high, 0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf0) + '">' + formatFinaArturnValue(row.cf0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf1) + '">' + formatFinaArturnValue(row.cf1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf2) + '">' + formatFinaArturnValue(row.cf2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf3) + '">' + formatFinaArturnValue(row.cf3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf4) + '">' + formatFinaArturnValue(row.cf4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf5) + '">' + formatFinaArturnValue(row.cf5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf6) + '">' + formatFinaArturnValue(row.cf6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf7) + '">' + formatFinaArturnValue(row.cf7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.cf8) + '">' + formatFinaArturnValue(row.cf8) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.oi_high, 0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi0) + '">' + formatFinaArturnValue(row.oi0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi1) + '">' + formatFinaArturnValue(row.oi1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi2) + '">' + formatFinaArturnValue(row.oi2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi3) + '">' + formatFinaArturnValue(row.oi3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi4) + '">' + formatFinaArturnValue(row.oi4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi5) + '">' + formatFinaArturnValue(row.oi5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi6) + '">' + formatFinaArturnValue(row.oi6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi7) + '">' + formatFinaArturnValue(row.oi7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oi8) + '">' + formatFinaArturnValue(row.oi8) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.dtp_high, 0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp0) + '">' + formatFinaArturnValue(row.dtp0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp1) + '">' + formatFinaArturnValue(row.dtp1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp2) + '">' + formatFinaArturnValue(row.dtp2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp3) + '">' + formatFinaArturnValue(row.dtp3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp4) + '">' + formatFinaArturnValue(row.dtp4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp5) + '">' + formatFinaArturnValue(row.dtp5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp6) + '">' + formatFinaArturnValue(row.dtp6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp7) + '">' + formatFinaArturnValue(row.dtp7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.dtp8) + '">' + formatFinaArturnValue(row.dtp8) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.fst_high, 0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst0) + '">' + formatFinaArturnValue(row.fst0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst1) + '">' + formatFinaArturnValue(row.fst1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst2) + '">' + formatFinaArturnValue(row.fst2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst3) + '">' + formatFinaArturnValue(row.fst3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst4) + '">' + formatFinaArturnValue(row.fst4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst5) + '">' + formatFinaArturnValue(row.fst5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst6) + '">' + formatFinaArturnValue(row.fst6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst7) + '">' + formatFinaArturnValue(row.fst7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.fst8) + '">' + formatFinaArturnValue(row.fst8) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.ocs_high, 0) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.ocs_wqcv) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs0) + '">' + formatFinaArturnOcfOrPct(row.ocs0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs1) + '">' + formatFinaArturnOcfOrPct(row.ocs1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs2) + '">' + formatFinaArturnOcfOrPct(row.ocs2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs3) + '">' + formatFinaArturnOcfOrPct(row.ocs3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs4) + '">' + formatFinaArturnOcfOrPct(row.ocs4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs5) + '">' + formatFinaArturnOcfOrPct(row.ocs5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs6) + '">' + formatFinaArturnOcfOrPct(row.ocs6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs7) + '">' + formatFinaArturnOcfOrPct(row.ocs7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.ocs8) + '">' + formatFinaArturnOcfOrPct(row.ocs8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco0) + '">' + formatFinaArturnOcfOrPct(row.oco0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco1) + '">' + formatFinaArturnOcfOrPct(row.oco1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco2) + '">' + formatFinaArturnOcfOrPct(row.oco2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco3) + '">' + formatFinaArturnOcfOrPct(row.oco3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco4) + '">' + formatFinaArturnOcfOrPct(row.oco4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco5) + '">' + formatFinaArturnOcfOrPct(row.oco5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco6) + '">' + formatFinaArturnOcfOrPct(row.oco6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco7) + '">' + formatFinaArturnOcfOrPct(row.oco7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.oco8) + '">' + formatFinaArturnOcfOrPct(row.oco8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r0) + '">' + formatFinaArturnValue(row.r0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r1) + '">' + formatFinaArturnValue(row.r1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r2) + '">' + formatFinaArturnValue(row.r2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r3) + '">' + formatFinaArturnValue(row.r3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r4) + '">' + formatFinaArturnValue(row.r4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r5) + '">' + formatFinaArturnValue(row.r5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r6) + '">' + formatFinaArturnValue(row.r6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r7) + '">' + formatFinaArturnValue(row.r7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.r8) + '">' + formatFinaArturnValue(row.r8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u0) + '">' + formatFinaArturnValue(row.u0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u1) + '">' + formatFinaArturnValue(row.u1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u2) + '">' + formatFinaArturnValue(row.u2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u3) + '">' + formatFinaArturnValue(row.u3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u4) + '">' + formatFinaArturnValue(row.u4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u5) + '">' + formatFinaArturnValue(row.u5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u6) + '">' + formatFinaArturnValue(row.u6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u7) + '">' + formatFinaArturnValue(row.u7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.u8) + '">' + formatFinaArturnValue(row.u8) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v0, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v1, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v2, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v3, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v4, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v5, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v6, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v7, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.v8, 2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.m_high) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.m_qoq) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m0) + '">' + formatFinaArturnValue(row.m0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m1) + '">' + formatFinaArturnValue(row.m1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m2) + '">' + formatFinaArturnValue(row.m2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m3) + '">' + formatFinaArturnValue(row.m3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m4) + '">' + formatFinaArturnValue(row.m4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m5) + '">' + formatFinaArturnValue(row.m5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m6) + '">' + formatFinaArturnValue(row.m6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m7) + '">' + formatFinaArturnValue(row.m7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.m8) + '">' + formatFinaArturnValue(row.m8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n0) + '">' + formatFinaArturnValue(row.n0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n1) + '">' + formatFinaArturnValue(row.n1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n2) + '">' + formatFinaArturnValue(row.n2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n3) + '">' + formatFinaArturnValue(row.n3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n4) + '">' + formatFinaArturnValue(row.n4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n5) + '">' + formatFinaArturnValue(row.n5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n6) + '">' + formatFinaArturnValue(row.n6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n7) + '">' + formatFinaArturnValue(row.n7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.n8) + '">' + formatFinaArturnValue(row.n8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t0) + '">' + formatFinaArturnValue(row.t0) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t1) + '">' + formatFinaArturnValue(row.t1) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t2) + '">' + formatFinaArturnValue(row.t2) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t3) + '">' + formatFinaArturnValue(row.t3) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t4) + '">' + formatFinaArturnValue(row.t4) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t5) + '">' + formatFinaArturnValue(row.t5) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t6) + '">' + formatFinaArturnValue(row.t6) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t7) + '">' + formatFinaArturnValue(row.t7) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(row.t8) + '">' + formatFinaArturnValue(row.t8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(computeInventoryChg(row)) + '">' + formatFinaArturnValue(computeInventoryChg(row)) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv_revenue_pct) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv_high, 0) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv0) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv3) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv4) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv5) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv6) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv7) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.inv8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(computeContractLiabChg(row)) + '">' + formatFinaArturnValue(computeContractLiabChg(row)) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k_revenue_pct) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k_high, 0) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k0) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k3) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k4) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k5) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k6) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k7) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.k8) + '</td>' +
                '<td class="median-table-num' + finaArturnNegClass(computeFixedAssetsChg(row)) + '">' + formatFinaArturnValue(computeFixedAssetsChg(row)) + '</td>' +
                '<td class="median-table-num"><span class="fina-fixed-assets-clickable" data-fixed-assets-ts-code="' + escapeHtmlAttr(row.ts_code || '') + '" data-fixed-assets-sec-name="' + escapeHtmlAttr(row.sec_name || '') + '" title="查看固定资产、在建工程与营收（最多25季）">' + formatFinaArturnValue(row.f0) + '</span></td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f2) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f3) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f4) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f5) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f6) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f7) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.f8) + '</td>' +
                '</tr>';
            return { f: fHtml, s: sHtml };
        };
        if (st._renderChunkRaf) {
            cancelAnimationFrame(st._renderChunkRaf);
            st._renderChunkRaf = 0;
        }
        var renderToken = (st._renderToken || 0) + 1;
        st._renderToken = renderToken;
        var frozenTbody = document.getElementById('fina-arturn-frozen-tbody');
        var totalAll = rows.length;
        /* 判断是否需要全量重建 DOM：
           - rows 已更新（_rowsVersion 不等于 _domBuiltRowsVersion）
           - trMap 缺失
           - tbody 容器状态异常（被"无匹配"占位替换过 / 初次加载） */
        var hasPlaceholder = false;
        var firstScrollTr = tbody.firstElementChild;
        if (firstScrollTr && firstScrollTr.querySelector &&
            firstScrollTr.querySelector('td[colspan]')) {
            hasPlaceholder = true;
        }
        var needRebuild = (st._domBuiltRowsVersion !== st._rowsVersion) ||
            !st._frozenTrMap || !st._scrollTrMap ||
            !frozenTbody || frozenTbody.children.length === 0 ||
            !firstScrollTr || hasPlaceholder;

        /* 快路径：DOM 已就绪时，仅 display 切换 + DocumentFragment 重排，不创建/销毁任何节点 */
        var applyFinaMetricFilterSort = function () {
            if (st._renderToken !== renderToken) return;
            var fMap = st._frozenTrMap;
            var sMap = st._scrollTrMap;
            if (!fMap || !sMap) return;
            var fTb = document.getElementById('fina-arturn-frozen-tbody');
            var sTb = document.getElementById(cfg.ids.tbody);
            if (!fTb || !sTb) return;
            var visible = Object.create(null);
            var fFrag = document.createDocumentFragment();
            var sFrag = document.createDocumentFragment();
            for (var vi = 0; vi < filtered.length; vi++) {
                var rv = filtered[vi];
                var idx = rv && rv._origIdx;
                if (idx == null) continue;
                visible[idx] = 1;
                var ftr = fMap[idx];
                var str = sMap[idx];
                var zebra = (vi % 2 === 1);
                if (ftr) {
                    if (ftr.style.display) ftr.style.display = '';
                    ftr.classList.toggle('median-table-zebra', zebra);
                    fFrag.appendChild(ftr);
                }
                if (str) {
                    if (str.style.display) str.style.display = '';
                    str.classList.toggle('median-table-zebra', zebra);
                    sFrag.appendChild(str);
                }
            }
            for (var hi = 0; hi < totalAll; hi++) {
                if (visible[hi]) continue;
                var fh = fMap[hi];
                var sh = sMap[hi];
                if (fh) {
                    if (fh.style.display !== 'none') fh.style.display = 'none';
                    fFrag.appendChild(fh);
                }
                if (sh) {
                    if (sh.style.display !== 'none') sh.style.display = 'none';
                    sFrag.appendChild(sh);
                }
            }
            fTb.appendChild(fFrag);
            sTb.appendChild(sFrag);
        };

        if (needRebuild) {
            /* 全量重建：按 st.rows 原始顺序分块渲染（不做筛选/排序），
               完成后一次性建立 tr Map，最后调用 applyFinaMetricFilterSort 应用当前筛选/排序 */
            var CHUNK_FIRST = 60;
            var CHUNK_NEXT = totalAll > 1000 ? 120 : 180;
            var buildFinaRowsChunk = function (start, end) {
                var fParts = new Array(end - start);
                var sParts = new Array(end - start);
                for (var bi = start; bi < end; bi++) {
                    /* origIdx=bi（全量顺序），zebraPos=bi（重建后即刻被 apply 覆盖，临时值无视觉副作用） */
                    var r = buildRowHtml(rows[bi], bi, bi);
                    fParts[bi - start] = r.f;
                    sParts[bi - start] = r.s;
                }
                return { f: fParts.join(''), s: sParts.join('') };
            };
            var firstEnd = Math.min(CHUNK_FIRST, totalAll);
            var firstChunk = buildFinaRowsChunk(0, firstEnd);
            if (frozenTbody) frozenTbody.innerHTML = firstChunk.f;
            tbody.innerHTML = firstChunk.s;
            var renderIdx = firstEnd;
            var finalizeRebuild = function () {
                if (st._renderToken !== renderToken) return;
                var fMap = new Array(totalAll);
                var sMap = new Array(totalAll);
                if (frozenTbody) {
                    var ftrs = frozenTbody.children;
                    for (var fi = 0; fi < ftrs.length; fi++) {
                        var origF = parseInt(ftrs[fi].getAttribute('data-row-index') || '-1', 10);
                        if (origF >= 0) fMap[origF] = ftrs[fi];
                    }
                }
                var strs = tbody.children;
                for (var si = 0; si < strs.length; si++) {
                    var origS = parseInt(strs[si].getAttribute('data-row-index') || '-1', 10);
                    if (origS >= 0) sMap[origS] = strs[si];
                }
                st._frozenTrMap = fMap;
                st._scrollTrMap = sMap;
                st._domBuiltRowsVersion = st._rowsVersion;
                st._renderChunkRaf = 0;
                applyFinaMetricFilterSort();
            };
            var finaRenderStep = function () {
                if (st._renderToken !== renderToken) return;
                if (renderIdx >= totalAll) {
                    finalizeRebuild();
                    return;
                }
                var nextEnd = Math.min(renderIdx + CHUNK_NEXT, totalAll);
                var chunk = buildFinaRowsChunk(renderIdx, nextEnd);
                if (frozenTbody) frozenTbody.insertAdjacentHTML('beforeend', chunk.f);
                tbody.insertAdjacentHTML('beforeend', chunk.s);
                renderIdx = nextEnd;
                st._renderChunkRaf = requestAnimationFrame(finaRenderStep);
            };
            if (renderIdx < totalAll) {
                st._renderChunkRaf = requestAnimationFrame(finaRenderStep);
            } else {
                finalizeRebuild();
            }
        } else {
            applyFinaMetricFilterSort();
        }
    }

    var msgEl = document.getElementById(cfg.ids.msg);
    if (msgEl) {
        var selected = st.selectedEndDate || '--';
        msgEl.textContent = '报告期：' + selected + '；共 ' + filtered.length + ' / ' + rows.length + ' 条';
    }
    renderFinaMetricHeaderFilterButtons(panelKey);
    if (panelKey === 'arturn') {
        if (!skipTheadRebuild) {
            requestAnimationFrame(function () {
                requestAnimationFrame(function () {
                    syncFinaArturnTheadStickyHeights();
                    syncFinaArturnColumnWidths();
                });
            });
        }
        var faOv = document.getElementById('fina-fixed-assets-modal-overlay');
        if (faOv && faOv.classList.contains('is-open')) {
            syncFinaFixedAssetsNavButtons();
        }
    }
}

function renderFinaArturnTable() {
    renderFinaMetricTable('arturn');
}

function fetchFinaMetricTable(panelKey, endDate) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return Promise.resolve({ ok: false });
    var st = _finaMetricState();
    var msgEl = document.getElementById(cfg.ids.msg);
    if (msgEl) msgEl.textContent = '加载中...';
    var url = cfg.apiPath;
    if (endDate) url += '?end_date=' + encodeURIComponent(endDate);
    var fullUrl = rtMonitorApiUrl(url);
    var ac = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var to = null;
    if (ac && FINA_METRIC_TABLE_FETCH_TIMEOUT_MS > 0) {
        to = setTimeout(function () {
            try {
                ac.abort();
            } catch (eAb) {
                /* ignore */
            }
        }, FINA_METRIC_TABLE_FETCH_TIMEOUT_MS);
    }
    var fetchOpts = { cache: 'no-store' };
    if (ac) fetchOpts.signal = ac.signal;
    return fetch(fullUrl, fetchOpts)
        .then(function (res) {
            if (!res.ok) {
                throw new Error('HTTP ' + res.status);
            }
            return res.json();
        })
        .then(function (data) {
            if (!data) {
                throw new Error('加载失败');
            }
            st.endDates = Array.isArray(data.end_dates) ? data.end_dates : [];
            st.periods = Array.isArray(data.periods) ? data.periods : [];
            st.contractPeriods = Array.isArray(data.contract_periods) ? data.contract_periods : [];
            st.selectedEndDate = data.selected_end_date || (st.endDates[0] || '');
            renderFinaMetricEndDateOptions(panelKey, st.endDates, st.selectedEndDate);
            renderFinaMetricSchemeApplyManualEndDateOptions(panelKey, st.endDates, st.selectedEndDate);
            saveFinaMetricViewState(panelKey);
            if (data.error) {
                st.rows = Array.isArray(data.rows) ? data.rows : [];
                markFinaMetricRowsChanged(st);
                renderFinaMetricTable(panelKey);
                var updatedErr = document.getElementById(cfg.ids.headUpdated);
                if (updatedErr) updatedErr.textContent = '更新：--';
                if (msgEl) msgEl.textContent = '加载失败：' + String(data.error);
                saveFinaMetricViewState(panelKey);
                return { ok: false };
            }
            st.rows = Array.isArray(data.rows) ? data.rows : [];
            markFinaMetricRowsChanged(st);
            renderFinaMetricTable(panelKey);
            var updatedEl = document.getElementById(cfg.ids.headUpdated);
            if (updatedEl) updatedEl.textContent = '更新：' + finaArturnFormatHeadDate(data.last_update);
            saveFinaMetricViewState(panelKey);
            return { ok: true };
        })
        .catch(function (err) {
            logError(cfg.logLabel + ' 请求失败', err);
            st.rows = [];
            markFinaMetricRowsChanged(st);
            var isAbort =
                err &&
                (err.name === 'AbortError' ||
                    (typeof err.message === 'string' && err.message.toLowerCase().indexOf('aborted') >= 0));
            var errText = isAbort
                ? '请求超时（' + String(FINA_METRIC_TABLE_FETCH_TIMEOUT_MS / 1000) + 's），请检查 MySQL/网络或稍后重试'
                : String(err && err.message ? err.message : err);
            renderFinaMetricTable(panelKey);
            if (msgEl) msgEl.textContent = '加载失败：' + errText;
            saveFinaMetricViewState(panelKey);
            return { ok: false };
        })
        .finally(function () {
            if (to !== null) clearTimeout(to);
            hideFinaBootHint();
        });
}

function fetchFinaArturnTable(endDate) {
    return fetchFinaMetricTable('arturn', endDate);
}

function closeFinaMetricFilterPopover(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var pop = document.getElementById(cfg.ids.filterPopover);
    if (!pop) return;
    pop.hidden = true;
    pop.style.position = '';
    pop.style.left = '';
    pop.style.top = '';
    pop.style.right = '';
    pop.style.bottom = '';
    _finaMetricState().activeFilterKey = '';
}

function closeFinaFilterPopover() {
    closeFinaMetricFilterPopover('arturn');
}

function setFinaMetricSchemeModalMsg(panelKey, text) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var el = document.getElementById(cfg.ids.schemeModalMsg);
    if (el) el.textContent = text || '';
}

function setFinaArturnSchemeModalMsg(text) {
    setFinaMetricSchemeModalMsg('arturn', text);
}

function closeFinaMetricSchemeModal(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    clearTimeout(finaSchemeModalSaveTimer);
    finaSchemeModalSaveTimer = null;
    var overlay = document.getElementById(cfg.ids.schemeOverlay);
    var wasOpen = overlay && overlay.classList.contains('is-open');
    var snap = null;
    /* 仅用本轮打开维护的 memory 持久化：关窗路径禁止同步 getBoundingClientRect（会逼 sticky 大表整页 reflow）。
       未命中 memory（极少：开盖瞬间即关且无存盘布局）则跳过写盘，下次打开仍用默认位置。 */
    if (
        wasOpen &&
        finaSchemeModalLayoutMemory &&
        finaSchemeModalLayoutMemory.w >= 200 &&
        finaSchemeModalLayoutMemory.h >= 120
    ) {
        snap = finaSchemeModalLayoutMemory;
    }
    if (overlay) overlay.classList.remove('is-open');
    if (typeof document !== 'undefined' && document.body) {
        document.body.classList.remove('fina-arturn-scheme-modal-open');
    }
    if (wasOpen) _modalClosed();
    if (snap) persistFinaSchemeModalLayoutSnapAsync(snap);
}

function closeFinaArturnSchemeModal() {
    closeFinaMetricSchemeModal('arturn');
}

function openFinaMetricSchemeModal(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var overlay = document.getElementById(cfg.ids.schemeOverlay);
    if (!overlay) return;
    /* 取消上一轮关窗后仍可能挂起的 ResizeObserver 防抖保存，避免关屏后仍调 getBoundingClientRect 叠主线程 */
    clearTimeout(finaSchemeModalSaveTimer);
    finaSchemeModalSaveTimer = null;
    finaSchemeModalLayoutOpenTs = Date.now();
    finaSchemeModalLayoutMemory = null;
    /* 先于 DOM 更新加上：让主表进入独立层，减轻随后 is-open 时整页与 sticky 连锁计算 */
    if (typeof document !== 'undefined' && document.body) {
        document.body.classList.add('fina-arturn-scheme-modal-open');
    }
    renderFinaMetricSchemeOptions(panelKey);
    var st = _finaMetricState();
    renderFinaMetricEndDateOptions(panelKey, st.endDates, st.selectedEndDate);
    renderFinaMetricSchemeApplyManualEndDateOptions(panelKey, st.endDates, st.selectedEndDate);
    syncFinaMetricStockScopeCheckboxes(panelKey);
    syncFinaMetricSchemeApplyModeUi(panelKey);
    setFinaMetricSchemeModalMsg(panelKey, '');
    overlay.classList.add('is-open');
    _modalOpened();
    /* 推迟到下一帧再应用存盘几何 / 测默认几何写 memory，避免与 is-open 同一帧叠两次布局 */
    requestAnimationFrame(applyFinaSchemeModalSavedLayout);
}

function openFinaArturnSchemeModal() {
    openFinaMetricSchemeModal('arturn');
}

function initFinaMetricSchemeModal(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var overlay = document.getElementById(cfg.ids.schemeOverlay);
    var closeBtn = document.getElementById(cfg.ids.schemeClose);
    var section = document.getElementById(cfg.ids.section);
    if (section && section.dataset.rtMonitorFinaSchemeDeleg !== '1') {
        section.dataset.rtMonitorFinaSchemeDeleg = '1';
        section.addEventListener('click', function (e) {
            var t = e.target;
            if (!t || !t.closest) return;
            var gear = t.closest('#' + cfg.ids.schemeOpen);
            if (gear) {
                e.stopPropagation();
                openFinaMetricSchemeModal(panelKey);
            }
        });
    }
    if (closeBtn && closeBtn.dataset.finaSchemeModalCloseBound !== '1') {
        closeBtn.dataset.finaSchemeModalCloseBound = '1';
        closeBtn.addEventListener('click', function () {
            closeFinaMetricSchemeModal(panelKey);
        });
    }
    if (overlay && overlay.dataset.finaSchemeOverlayClickBound !== '1') {
        overlay.dataset.finaSchemeOverlayClickBound = '1';
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeFinaMetricSchemeModal(panelKey);
        });
    }
    /* Esc 全局只绑一次（若未来 init 被重复调用，避免监听器倍增） */
    if (!document.documentElement.dataset.rtMonitorFinaSchemeEscBound) {
        document.documentElement.dataset.rtMonitorFinaSchemeEscBound = '1';
        document.addEventListener('keydown', function (ev) {
            if (!ev || ev.key !== 'Escape') return;
            if (!overlay || !overlay.classList.contains('is-open')) return;
            closeFinaMetricSchemeModal(panelKey);
        });
    }
    if (overlay && overlay.dataset.finaSchemeModalLayoutBound !== '1') {
        overlay.dataset.finaSchemeModalLayoutBound = '1';
        var schemeModal = overlay.querySelector('.fina-arturn-scheme-modal-shell');
        var schemeHeader = overlay.querySelector('.bias-series-modal-header');
        if (schemeModal && schemeHeader) {
            schemeModal.style.position = 'fixed';
            schemeModal.style.left = '50%';
            schemeModal.style.top = '50%';
            schemeModal.style.transform = 'translate(-50%, -50%)';
            var schemeDragging = false;
            var schemeStartX = 0;
            var schemeStartY = 0;
            var schemeStartLeft = 0;
            var schemeStartTop = 0;
            var schemeDragW = 0;
            var schemeDragH = 0;
            schemeHeader.style.cursor = 'move';
            schemeHeader.addEventListener('mousedown', function (e) {
                if (e.button !== 0) return;
                schemeDragging = true;
                schemeStartX = e.clientX;
                schemeStartY = e.clientY;
                var rect = schemeModal.getBoundingClientRect();
                schemeStartLeft = rect.left;
                schemeStartTop = rect.top;
                schemeDragW = rect.width;
                schemeDragH = rect.height;
                schemeModal.style.transform = '';
                e.preventDefault();
            });
            document.addEventListener('mousemove', function (e) {
                if (!schemeDragging) return;
                var dx = e.clientX - schemeStartX;
                var dy = e.clientY - schemeStartY;
                var newLeft = schemeStartLeft + dx;
                var newTop = schemeStartTop + dy;
                var vw = window.innerWidth || document.documentElement.clientWidth;
                var vh = window.innerHeight || document.documentElement.clientHeight;
                var maxLeft = vw - schemeDragW;
                var maxTop = vh - schemeDragH;
                if (newLeft < 0) newLeft = 0;
                if (newTop < 0) newTop = 0;
                if (newLeft > maxLeft) newLeft = maxLeft;
                if (newTop > maxTop) newTop = maxTop;
                schemeModal.style.left = newLeft + 'px';
                schemeModal.style.top = newTop + 'px';
            });
            document.addEventListener('mouseup', function () {
                if (schemeDragging) saveFinaSchemeModalLayout(schemeModal);
                schemeDragging = false;
            });
        }
        if (schemeModal && schemeModal.classList && schemeModal.classList.contains('fina-arturn-scheme-modal-shell') && typeof ResizeObserver !== 'undefined') {
            var roSchemeShell = new ResizeObserver(function () {
                /* 关窗后 display:none 仍会偶发 resize 回调，若再 schedule 会在主表上叠 getBoundingClientRect */
                if (!overlay || !overlay.classList.contains('is-open')) return;
                scheduleSaveFinaSchemeModalLayout(schemeModal);
            });
            roSchemeShell.observe(schemeModal);
        }
    }
}

function initFinaArturnSchemeModal() {
    initFinaMetricSchemeModal('arturn');
}

/**
 * 与列筛选弹窗标题同源：供已保存方案摘要展示（依赖当前 periods / contractPeriods）。
 * panelKey 预留与 FINA_METRIC_PANEL 对齐；当前仅 arturn。
 */
function computeFinaMetricFilterKeyTitle(panelKey, filterKey) {
    void panelKey;
    var numHint = '（支持 >=30、<50、30-80）';
    var labelMap = {
        tsCode: '股票代码（包含匹配）',
        secName: '股票名称（包含匹配）',
        swName: '申万二级行业（包含匹配；多条件用英文 or 分隔，或关系）'
    };
    var title = labelMap[filterKey];
    if (!title && /^roe[0-3]$/.test(filterKey)) {
        var idxRoe = parseInt(filterKey.slice(3), 10);
        var periodsRoe = Array.isArray(_finaMetricState().periods) ? _finaMetricState().periods : [];
        var rawRoe = periodsRoe[idxRoe];
        var periodLabelRoe;
        if (rawRoe && /^\d{8}$/.test(String(rawRoe))) {
            periodLabelRoe = finaArturnPeriodDisplayYear(rawRoe);
        } else {
            var fbRoe = ['当期', '上一年', '前两年', '前三年'];
            periodLabelRoe = fbRoe[idxRoe] || filterKey;
        }
        title = 'ROE（扣非）·' + periodLabelRoe + numHint;
    } else if (!title && /^a0$/.test(filterKey)) {
        title = '公告日（支持包含匹配）';
    } else if (!title && /^listDate$/.test(filterKey)) {
        title = '上市日（支持包含匹配）';
    } else if (!title && filterKey === 'total_mv_yi') {
        title = '总市值（亿元，stk_factor_pro.total_mv 万元/10000；表头不显示单位）' + numHint;
    } else if (!title && filterKey === 'free_mv_yi') {
        title = '自由市值（亿元，free_share×rt_k.close；表头不显示单位）' + numHint;
    } else if (!title && filterKey === 'money_cap_ratio_pct') {
        title = '市值/货币（最新报告期 money_cap_opt ÷ 总市值 ×100）' + numHint;
    } else if (!title && filterKey === 'fqm_debt_to_assets') {
        title = '资产负债率（fina_quarter_metrics.debt_to_assets，与表头所选报告期 end_date 对齐）' + numHint;
    } else if (!title && filterKey === 'fqm_interestdebt_ratio') {
        title = '带息负债率（fina_quarter_metrics.interestdebt_ratio，与表头所选报告期 end_date 对齐）' + numHint;
    } else if (!title && filterKey === 'pe_ttm') {
        title = 'PE（stk_factor_pro.pe_ttm，最新 trade_date）' + numHint;
    } else if (!title && filterKey === 'ttm_revenue_yi') {
        title = '营收·最近四单季营业总收入之和（亿元，表头不显示单位）' + numHint;
    } else if (!title && filterKey === 'ar_pct') {
        title = '应收%·TTM 单季营收' + numHint;
    } else if (!title && /^kchg$/.test(filterKey)) {
        title = '合同负债（亿）·Chg%（当期÷近8季已有值算术均值-1）' + numHint;
    } else if (!title && /^invchg$/.test(filterKey)) {
        title = '存货（亿）·Chg%（当期÷近8季已有值算术均值-1）' + numHint;
    } else if (!title && filterKey === 'inv_revenue_pct') {
        title = '营收%（当期存货 inv0 / 营收 TTM，亿口径相除×100）' + numHint;
    } else if (!title && filterKey === 'k_revenue_pct') {
        title = '营收%（当期合同负债 k0 / 营收 TTM，亿口径相除×100）' + numHint;
    } else if (!title && /^fchg$/.test(filterKey)) {
        title = '固定资产（亿）·Chg%（当期÷近8季已有值算术均值-1）' + numHint;
    } else if (!title && /^k[0-8]$/.test(filterKey)) {
        var idxK = parseInt(filterKey.charAt(1), 10);
        var cpsK = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawK = cpsK[idxK];
        title = '合同负债（亿）·' + finaArturnQuarterLabel(rawK) + numHint;
    } else if (!title && /^f[0-8]$/.test(filterKey)) {
        var idxF = parseInt(filterKey.charAt(1), 10);
        var cpsF = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawF = cpsF[idxF];
        title = '固定资产（亿）·' + finaArturnQuarterLabel(rawF) + numHint;
    } else if (!title && /^(m|n|t)[0-8]$/.test(filterKey)) {
        var idxMn = parseInt(filterKey.charAt(1), 10);
        var cpsMn = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawMn = cpsMn[idxMn];
        var isM = filterKey.charAt(0) === 'm';
        var isN = filterKey.charAt(0) === 'n';
        title = (isM ? '单季度毛利率·' : isN ? '单季度净利率·' : '单季度ROE(扣非)·') + finaArturnQuarterLabel(rawMn) + numHint;
    } else if (!title && filterKey === 's_high') {
        title = '新高（1=最新季 s0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'cf_high') {
        title = '新高（1=最新季 cf0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'oi_high') {
        title = '新高（1=最新季 oi0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'dtp_high') {
        title = '新高（1=最新季 dtp0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'fst_high') {
        title = '新高（1=最新季 fst0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'ocs_high') {
        title = '新高（1=最新季 ocs0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'm_high') {
        title = '新高（1=最新季 m0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'm_qoq') {
        title = '环比连增（m0>m1 记1；若 m1>m2 再+1，最多 8）' + numHint;
    } else if (!title && filterKey === 'ocs_wqcv') {
        title = '加权离散（WQCV）：九季 ocs 的加权变异系数，最新季权重最大（衰减默认 0.88/季）；数值越小相对波动越小；有效季少于 2 为空；均值近 0 时为加权标准差' + numHint;
    } else if (!title && filterKey === 'inv_high') {
        title = '新高（1=最新季 inv0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && filterKey === 'k_high') {
        title = '新高（1=最新季 k0 在已有值的展示季中为最大，0=否；不要求 9 季全有值）' + numHint;
    } else if (!title && /^cf[0-8]$/.test(filterKey)) {
        var idxCf = parseInt(filterKey.slice(2), 10);
        var cpsCf = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawCf = cpsCf[idxCf];
        title = '单季度经营现金流净额（亿）·cashflow.n_cashflow_act 累计拆解·' + finaArturnQuarterLabel(rawCf) + numHint;
    } else if (!title && /^s[0-8]$/.test(filterKey)) {
        var idxS = parseInt(filterKey.charAt(1), 10);
        var cpsS = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawS = cpsS[idxS];
        title = '单季度营业收入（亿）·' + finaArturnQuarterLabel(rawS) + numHint;
    } else if (!title && /^oi[0-8]$/.test(filterKey)) {
        var idxOi = parseInt(filterKey.slice(2), 10);
        var cpsOi = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawOi = cpsOi[idxOi];
        title = '单季度经营活动净收益（亿）·fina_indicator.q_opincome·' + finaArturnQuarterLabel(rawOi) + numHint;
    } else if (!title && /^dtp[0-8]$/.test(filterKey)) {
        var idxDtp = parseInt(filterKey.slice(3), 10);
        var cpsDtp = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawDtp = cpsDtp[idxDtp];
        title = '单季度扣非净利润（亿）·fina_indicator.q_dtprofit·' + finaArturnQuarterLabel(rawDtp) + numHint;
    } else if (!title && /^fst[0-8]$/.test(filterKey)) {
        var idxFst = parseInt(filterKey.slice(3), 10);
        var cpsFst = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawFst = cpsFst[idxFst];
        title = '单季度 FCF（亿）·fina_quarter_metrics.q_fcf_star（FCF*=OCF−CAPEX）·' + finaArturnQuarterLabel(rawFst) + numHint;
    } else if (!title && /^ocs[0-8]$/.test(filterKey)) {
        var idxOcs = parseInt(filterKey.slice(3), 10);
        var cpsOcs = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawOcs = cpsOcs[idxOcs];
        title = '单季度经营现金流净额/营业收入(%)·' + finaArturnQuarterLabel(rawOcs) + numHint;
    } else if (!title && /^oco[0-8]$/.test(filterKey)) {
        var idxOco = parseInt(filterKey.slice(3), 10);
        var cpsOco = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawOco = cpsOco[idxOco];
        title = '单季度经营现金流净额/经营活动净收益(%)·' + finaArturnQuarterLabel(rawOco) + numHint;
    } else if (!title && /^r[0-8]$/.test(filterKey)) {
        var idxGr = parseInt(filterKey.charAt(1), 10);
        var cpsGr = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawGr = cpsGr[idxGr];
        title = '单季度营业收入同比增速(%)·fina_indicator.q_gr_yoy·' + finaArturnQuarterLabel(rawGr) + numHint;
    } else if (!title && /^u[0-8]$/.test(filterKey)) {
        var idxOp = parseInt(filterKey.charAt(1), 10);
        var cpsOp = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawOp = cpsOp[idxOp];
        title = '单季度营业利润同比增长率(%)·fina_indicator.q_op_yoy·' + finaArturnQuarterLabel(rawOp) + numHint;
    } else if (!title && /^v[0-8]$/.test(filterKey)) {
        var idxVx = parseInt(filterKey.charAt(1), 10);
        var cpsVx = Array.isArray(_finaMetricState().contractPeriods) ? _finaMetricState().contractPeriods : [];
        var rawVx = cpsVx[idxVx];
        title = '单季度利润增速高于收入增速(百分点)·q_op_yoy−q_gr_yoy（与表内 r/u 同比列同源，非利润绝对额）·' + finaArturnQuarterLabel(rawVx) + numHint;
    } else if (!title && /^[pgco][0-3]$/.test(filterKey)) {
        var idxPg = parseInt(filterKey.charAt(1), 10);
        var chPg = filterKey.charAt(0);
        var isP = chPg === 'p';
        var isG = chPg === 'g';
        var isC = chPg === 'c';
        var isO = chPg === 'o';
        var periodsPg = Array.isArray(_finaMetricState().periods) ? _finaMetricState().periods : [];
        var rawPg = periodsPg[idxPg];
        var periodLabelPg;
        if (rawPg && /^\d{8}$/.test(String(rawPg))) {
            periodLabelPg = finaArturnPeriodDisplayYear(rawPg);
        } else {
            var fbPg = ['当期', '上一年', '前两年', '前三年'];
            periodLabelPg = fbPg[idxPg] || filterKey;
        }
        title = (isP ? '应收账款周转天数（天）·' : isG ? '销售毛利率·' : isC ? '经营现金流净额/经营活动净收益·' : isO ? '经营现金流净额/营业收入(%)·' : '') + periodLabelPg + numHint;
    }
    if (!title) {
        title = '未映射列（键 ' + filterKey + '，请对照表头或硬刷新脚本）';
    }
    return title;
}

/** 方案摘要里去掉弹窗尾部的输入格式提示，缩短行宽 */
function shortenFinaMetricFilterTitleForSchemeSummary(fullTitle) {
    return String(fullTitle || '')
        .replace(/（支持[^）]*）\s*$/g, '')
        .trim();
}

/** 下拉选中已保存方案时，展示其列筛选条件、排序与保存的报告期 */
function refreshFinaMetricSchemeSummary(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg || !cfg.ids.schemePreviewWrap || !cfg.ids.schemePreviewBody) return;
    var wrap = document.getElementById(cfg.ids.schemePreviewWrap);
    var body = document.getElementById(cfg.ids.schemePreviewBody);
    var sel = document.getElementById(cfg.ids.schemeSelect);
    if (!wrap || !body) return;
    var name = sel ? String(sel.value || '').trim() : '';
    if (!name) {
        wrap.hidden = true;
        body.innerHTML = '';
        return;
    }
    var schemes = loadFinaMetricSchemes(panelKey);
    var scheme = schemes[name];
    if (!scheme) {
        wrap.hidden = true;
        body.innerHTML = '';
        return;
    }
    wrap.hidden = false;
    var filters = normalizeFinaMetricFilterShape(scheme.filters || {});
    var keys = Object.keys(filters).filter(function (k) {
        return String(filters[k] || '').trim() !== '';
    }).sort();
    var lines = [];
    keys.forEach(function (k) {
        var fieldTitle = shortenFinaMetricFilterTitleForSchemeSummary(computeFinaMetricFilterKeyTitle(panelKey, k));
        var cond = String(filters[k] || '').trim();
        lines.push(
            '<div class="fina-arturn-scheme-preview-line">' +
                '<span class="fina-arturn-scheme-preview-field">' + escapeHtmlAttr(fieldTitle) + '</span>' +
                '<span class="fina-arturn-scheme-preview-sep">：</span>' +
                '<code class="fina-arturn-scheme-preview-val">' + escapeHtmlAttr(cond) + '</code>' +
            '</div>'
        );
    });
    var sk = String(scheme.sortKey || '').trim();
    if (sk && finaArturnIsSortableColumnKey(sk)) {
        var sortLabel = shortenFinaMetricFilterTitleForSchemeSummary(computeFinaMetricFilterKeyTitle(panelKey, sk));
        var dirZh = scheme.sortDir === 'desc' ? '降序' : '升序';
        lines.push(
            '<div class="fina-arturn-scheme-preview-line fina-arturn-scheme-preview-meta">' +
                '<span class="fina-arturn-scheme-preview-field">排序</span>' +
                '<span class="fina-arturn-scheme-preview-sep">：</span>' +
                '<code class="fina-arturn-scheme-preview-val">' + escapeHtmlAttr(sortLabel + ' · ' + dirZh) + '</code>' +
            '</div>'
        );
    }
    var ed = String(scheme.endDate || '').trim();
    if (ed) {
        lines.push(
            '<div class="fina-arturn-scheme-preview-line fina-arturn-scheme-preview-meta">' +
                '<span class="fina-arturn-scheme-preview-field">保存的报告期</span>' +
                '<span class="fina-arturn-scheme-preview-sep">：</span>' +
                '<code class="fina-arturn-scheme-preview-val">' + escapeHtmlAttr(ed) + '</code>' +
            '</div>'
        );
    }
    if (!lines.length) {
        body.innerHTML =
            '<div class="fina-arturn-scheme-preview-empty">该方案未保存列筛选条件（可与排序、报告期一并使用）。</div>';
    } else {
        body.innerHTML = lines.join('');
    }
}

function openFinaMetricFilterPopover(panelKey, filterKey, btnEl, anchorEvt) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var pop = document.getElementById(cfg.ids.filterPopover);
    var titleEl = document.getElementById(cfg.ids.filterTitle);
    var inputEl = document.getElementById(cfg.ids.filterInput);
    if (!pop || !titleEl || !inputEl || !btnEl) return;

    /* Portal：首次使用时把 popover（及其配套 datalist）提升到 <body> 顶层。
       原位置位于 <section class="fina-arturn-wrap"> 内，此 section 子树含 6000 行 × 187 列 tr，
       Chrome 在 click/focus/样式匹配时会沿 DOM 树做祖先/子树评估，大子树会引起显著卡顿。
       position:fixed 只解决布局，不解决 DOM 树位置；必须手动搬到 body 才能彻底隔离。 */
    if (pop.parentElement !== document.body) {
        document.body.appendChild(pop);
    }
    var dlHoist = document.getElementById('fina-filter-popover-dl');
    if (dlHoist && dlHoist.parentElement !== document.body) {
        document.body.appendChild(dlHoist);
    }
    var title = computeFinaMetricFilterKeyTitle(panelKey, filterKey);
    var filters = getFinaMetricFilters(panelKey);
    titleEl.textContent = title;

    /* 候选下拉：按列类型过滤历史值，写入共享 datalist
       注意：<datalist> 仅 option 列表，浏览器会做模糊匹配 + 键盘导航，不抢焦点 */
    var dl = document.getElementById('fina-filter-popover-dl');
    if (dl) {
        var histList = getFinaFilterHistoryFor(filterKey);
        if (!dl._boundKey || dl._boundKey !== filterKey ||
            (dl.children.length !== histList.length)) {
            /* 仅在 key 或数量变化时重建，避免频繁 innerHTML */
            var html = '';
            for (var hi = 0; hi < histList.length; hi++) {
                html += '<option value="' + escapeHtmlAttr(histList[hi]) + '"></option>';
            }
            dl.innerHTML = html;
            dl._boundKey = filterKey;
        }
    }

    /* 默认值策略：
        ① 已有当前筛选 → 填当前
        ② 否则取最近一次符合类型的历史值
        ③ 否则留空 */
    var curVal = String(filters[filterKey] || '').trim();
    var defaultVal = curVal;
    if (!defaultVal) {
        var hist = getFinaFilterHistoryFor(filterKey);
        if (hist.length) defaultVal = hist[0];
    }
    inputEl.value = defaultVal;
    _finaMetricState().activeFilterKey = filterKey;

    /* 性能优先：优先使用点击事件坐标锚定，避免每次点筛选都触发布局读取（getBoundingClientRect）。
       无事件坐标时再退化到元素几何。 */
    var pad = 8;
    var vw = window.innerWidth || document.documentElement.clientWidth || 0;
    var vh = window.innerHeight || document.documentElement.clientHeight || 0;
    pop.style.position = 'fixed';
    pop.style.right = 'auto';
    pop.style.bottom = 'auto';
    var desiredLeft = 0;
    var desiredTop = 0;
    if (anchorEvt && typeof anchorEvt.clientX === 'number' && typeof anchorEvt.clientY === 'number') {
        desiredLeft = anchorEvt.clientX - 200;
        desiredTop = anchorEvt.clientY + 8;
    } else {
        var btnRect = btnEl.getBoundingClientRect();
        desiredLeft = btnRect.left - 200;
        desiredTop = btnRect.bottom + 6;
    }
    pop.style.left = Math.max(pad, desiredLeft) + 'px';
    pop.style.top = Math.max(pad, desiredTop) + 'px';
    pop.style.visibility = 'hidden';
    pop.hidden = false;
    /* 固定近似尺寸做边界夹取，避免 offsetWidth/offsetHeight 造成二次强制回流。 */
    var popW = 292;
    var popH = 136;
    var maxLeft = Math.max(pad, vw - popW - pad);
    var leftClamped = Math.max(pad, Math.min(desiredLeft, maxLeft));
    var topClamped = Math.max(pad, Math.min(desiredTop, vh - popH - pad));
    pop.style.left = leftClamped + 'px';
    pop.style.top = topClamped + 'px';
    pop.style.visibility = '';
    requestAnimationFrame(function () {
        try { inputEl.focus({ preventScroll: true }); } catch (e) { inputEl.focus(); }
        inputEl.select();
    });
}

function openFinaFilterPopover(filterKey, btnEl, anchorEvt) {
    openFinaMetricFilterPopover('arturn', filterKey, btnEl, anchorEvt);
}

function initFinaMetricPanel(panelKey) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var ids = cfg.ids;
    var endDateSelect = document.getElementById(ids.schemeEndDate);
    var saveBtn = document.getElementById(ids.schemeSave);
    var applyBtn = document.getElementById(ids.schemeApply);
    var deleteBtn = document.getElementById(ids.schemeDelete);
    var resetFiltersBtn = document.getElementById(ids.schemeResetFilters);
    var schemeSelectEl = document.getElementById(ids.schemeSelect);
    var applyModeSelect = document.getElementById(ids.schemeApplyEndDateMode);
    var applyManualSelect = document.getElementById(ids.schemeApplyEndDateSelect);
    var applyRememberCheckbox = document.getElementById(ids.schemeApplyRememberPref);
    var tableHead = document.getElementById(ids.thead);
    var tbody = document.getElementById(ids.tbody);
    var splitWrap = document.getElementById(ids.splitWrap);
    var popInput = document.getElementById(ids.filterInput);
    var popApply = document.getElementById(ids.filterApply);
    var popClear = document.getElementById(ids.filterClear);
    var popClose = document.getElementById(ids.filterClose);

    var persistedView = loadFinaMetricViewState(panelKey);
    setFinaMetricFilters(panelKey, persistedView ? persistedView.filters : {});
    _finaMetricState().sortKey = (persistedView && finaArturnIsSortableColumnKey(persistedView.sortKey)) ? persistedView.sortKey : '';
    _finaMetricState().sortDir = (persistedView && persistedView.sortDir === 'desc') ? 'desc' : 'asc';
    _finaMetricState().selectedEndDate = persistedView ? String(persistedView.selectedEndDate || '') : '';
    _finaMetricState().excludeBj = persistedView ? persistedView.excludeBj === true : false;
    _finaMetricState().excludeSt = persistedView ? persistedView.excludeSt === true : false;
    var applyPref = loadFinaMetricSchemeApplyPref(panelKey);
    if (applyModeSelect) {
        applyModeSelect.value = applyPref ? String(applyPref.mode || 'current') : 'current';
        syncFinaMetricSchemeApplyModeUi(panelKey);
        applyModeSelect.addEventListener('change', function () {
            syncFinaMetricSchemeApplyModeUi(panelKey);
        });
    }
    if (applyRememberCheckbox) {
        applyRememberCheckbox.checked = false;
    }
    if (applyManualSelect && applyPref && applyPref.manualEndDate) {
        applyManualSelect.dataset.prefEndDate = String(applyPref.manualEndDate || '');
    }
    if (endDateSelect) {
        endDateSelect.addEventListener('change', function () {
            var val = String(this.value || '').trim();
            _finaMetricState().selectedEndDate = val;
            saveFinaMetricViewState(panelKey);
            fetchFinaMetricTable(panelKey, val);
        });
    }
    if (schemeSelectEl && !schemeSelectEl.dataset.boundSchemeSummary) {
        schemeSelectEl.dataset.boundSchemeSummary = '1';
        schemeSelectEl.addEventListener('change', function () {
            refreshFinaMetricSchemeSummary(panelKey);
        });
    }
    var excludeBjCb = document.getElementById(ids.excludeBj);
    var excludeStCb = document.getElementById(ids.excludeSt);
    if (excludeBjCb && !excludeBjCb.dataset.boundStockScope) {
        excludeBjCb.dataset.boundStockScope = '1';
        excludeBjCb.addEventListener('change', function () {
            _finaMetricState().excludeBj = !!this.checked;
            renderFinaMetricTable(panelKey);
            saveFinaMetricViewState(panelKey);
        });
    }
    if (excludeStCb && !excludeStCb.dataset.boundStockScope) {
        excludeStCb.dataset.boundStockScope = '1';
        excludeStCb.addEventListener('change', function () {
            _finaMetricState().excludeSt = !!this.checked;
            renderFinaMetricTable(panelKey);
            saveFinaMetricViewState(panelKey);
        });
    }
    syncFinaMetricStockScopeCheckboxes(panelKey);
    if (tableHead) {
        tableHead.addEventListener('click', function (ev) {
            var t = ev.target;
            if (!t || !t.classList) return;
            if (t.classList.contains('fina-sort-btn')) {
                var sk = t.getAttribute('data-sort-key') || '';
                if (sk) {
                    finaMetricCycleSort(sk);
                    renderFinaMetricTable(panelKey);
                    saveFinaMetricViewState(panelKey);
                }
                return;
            }
            if (!t.classList.contains('fina-filter-btn')) return;
            var key = t.getAttribute('data-filter-key') || '';
            openFinaMetricFilterPopover(panelKey, key, t, ev);
        });
    }
    if (splitWrap && !splitWrap.dataset.fixedAssetsClickBound) {
        splitWrap.dataset.fixedAssetsClickBound = '1';
        splitWrap.addEventListener('click', function (ev) {
            var t = ev.target;
            if (!t || !t.closest) return;
            var hit = t.closest('.fina-fixed-assets-clickable');
            if (!hit) return;
            var tsCode = String(hit.getAttribute('data-fixed-assets-ts-code') || '').trim();
            if (!tsCode || tsCode === '--') return;
            var secName = String(hit.getAttribute('data-fixed-assets-sec-name') || '').trim();
            openFinaFixedAssetsModal(tsCode, secName);
        });
    }
    if (popApply) {
        popApply.addEventListener('click', function () {
            var k = _finaMetricState().activeFilterKey || '';
            if (!k) return;
            var f = getFinaMetricFilters(panelKey);
            var val = popInput ? String(popInput.value || '').trim() : '';
            f[k] = val;
            setFinaMetricFilters(panelKey, f);
            /* 写入 per-key 历史（LRU 去重，上限 12 条）；仅在值符合列类型时写入，
               避免把"申万列误用的纯数字"这类脏数据污染到数值列 */
            if (val) {
                var typeOk = isFinaFilterValueValid(val, finaMetricFilterKeyType(k));
                if (typeOk) pushFinaFilterHistory(k, val);
            }
            closeFinaMetricFilterPopover(panelKey);
            renderFinaMetricTable(panelKey);
            saveFinaMetricViewState(panelKey);
        });
    }
    if (popClear) {
        popClear.addEventListener('click', function () {
            var k = _finaMetricState().activeFilterKey || '';
            if (!k) return;
            var f = getFinaMetricFilters(panelKey);
            f[k] = '';
            setFinaMetricFilters(panelKey, f);
            closeFinaMetricFilterPopover(panelKey);
            renderFinaMetricTable(panelKey);
            saveFinaMetricViewState(panelKey);
        });
    }
    if (popClose) {
        popClose.addEventListener('click', function () {
            closeFinaMetricFilterPopover(panelKey);
        });
    }
    document.addEventListener('click', function (ev) {
        var pop = document.getElementById(ids.filterPopover);
        if (!pop || pop.hidden) return;
        var t = ev.target;
        if (pop.contains(t)) return;
        if (t && t.classList && t.classList.contains('fina-filter-btn')) return;
        closeFinaMetricFilterPopover(panelKey);
    });
    if (popInput) {
        popInput.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter') {
                if (popApply) popApply.click();
            } else if (ev.key === 'Escape') {
                closeFinaMetricFilterPopover(panelKey);
            }
        });
    }
    if (saveBtn) {
        saveBtn.addEventListener('click', function () {
            var nameEl = document.getElementById(ids.schemeName);
            var name = nameEl ? String(nameEl.value || '').trim() : '';
            if (!name) {
                setFinaMetricSchemeModalMsg(panelKey, '请先输入筛选方案名称。');
                return;
            }
            var schemes = loadFinaMetricSchemes(panelKey);
            var st = _finaMetricState();
            schemes[name] = {
                endDate: st.selectedEndDate || '',
                filters: getFinaMetricFilters(panelKey),
                sortKey: String(st.sortKey || ''),
                sortDir: st.sortDir === 'desc' ? 'desc' : 'asc'
            };
            saveFinaMetricSchemes(panelKey, schemes);
            renderFinaMetricSchemeOptions(panelKey);
            setFinaMetricSchemeModalMsg(panelKey, '已保存筛选方案：' + name);
            saveFinaMetricViewState(panelKey);
        });
    }
    if (applyBtn) {
        applyBtn.addEventListener('click', function () {
            var sel = document.getElementById(ids.schemeSelect);
            var name = sel ? String(sel.value || '').trim() : '';
            if (!name) {
                setFinaMetricSchemeModalMsg(panelKey, '请先选择要调用的方案。');
                return;
            }
            var schemes = loadFinaMetricSchemes(panelKey);
            var scheme = schemes[name];
            if (!scheme) {
                setFinaMetricSchemeModalMsg(panelKey, '方案不存在，可能已被删除。');
                return;
            }
            setFinaMetricFilters(panelKey, scheme.filters || {});
            var st = _finaMetricState();
            st.sortKey = finaArturnIsSortableColumnKey(String(scheme.sortKey || '')) ? String(scheme.sortKey || '') : '';
            st.sortDir = scheme.sortDir === 'desc' ? 'desc' : 'asc';
            var applyMode = applyModeSelect ? String(applyModeSelect.value || 'current') : 'current';
            var manualEndDate = applyManualSelect ? String(applyManualSelect.value || '').trim() : '';
            var targetEndDate = '';
            var fallbackHint = '';
            if (applyMode === 'scheme') {
                var schemeEndDate = String(scheme.endDate || '').trim();
                if (schemeEndDate) {
                    targetEndDate = schemeEndDate;
                } else {
                    targetEndDate = String(st.selectedEndDate || '');
                    if (targetEndDate) {
                        fallbackHint = '（方案未保存报告期，已回退当前报告期）';
                    }
                }
            } else if (applyMode === 'manual') {
                if (manualEndDate) {
                    targetEndDate = manualEndDate;
                } else {
                    targetEndDate = String(st.selectedEndDate || String(scheme.endDate || '') || '');
                    if (targetEndDate) {
                        fallbackHint = '（手动模式未选择报告期，已回退当前/方案报告期）';
                    }
                }
            } else {
                targetEndDate = st.selectedEndDate || String(scheme.endDate || '');
            }
            if (applyRememberCheckbox && applyRememberCheckbox.checked) {
                saveFinaMetricSchemeApplyPref(panelKey, {
                    mode: applyMode,
                    manualEndDate: manualEndDate
                });
            }
            if (targetEndDate && targetEndDate !== st.selectedEndDate) {
                fetchFinaMetricTable(panelKey, targetEndDate).then(function () {
                    renderFinaMetricTable(panelKey);
                    saveFinaMetricViewState(panelKey);
                });
            } else {
                renderFinaMetricTable(panelKey);
                saveFinaMetricViewState(panelKey);
            }
            setFinaMetricSchemeModalMsg(panelKey, '已调用筛选方案：' + name + fallbackHint);
        });
    }
    if (deleteBtn) {
        deleteBtn.addEventListener('click', function () {
            var sel = document.getElementById(ids.schemeSelect);
            var name = sel ? String(sel.value || '').trim() : '';
            if (!name) {
                setFinaMetricSchemeModalMsg(panelKey, '请先选择要删除的方案。');
                return;
            }
            var schemes = loadFinaMetricSchemes(panelKey);
            if (!schemes[name]) {
                setFinaMetricSchemeModalMsg(panelKey, '方案不存在。');
                return;
            }
            delete schemes[name];
            saveFinaMetricSchemes(panelKey, schemes);
            renderFinaMetricSchemeOptions(panelKey);
            setFinaMetricSchemeModalMsg(panelKey, '已删除筛选方案：' + name);
        });
    }
    if (resetFiltersBtn) {
        resetFiltersBtn.addEventListener('click', function () {
            setFinaMetricFilters(panelKey, {});
            _finaMetricState().sortKey = '';
            _finaMetricState().sortDir = 'asc';
            closeFinaMetricFilterPopover(panelKey);
            setFinaMetricSchemeModalMsg(panelKey, '已清空所有列筛选条件并重置排序');
            renderFinaMetricTable(panelKey);
            saveFinaMetricViewState(panelKey);
        });
    }
    initFinaMetricSchemeModal(panelKey);
    (function initFixedAssetsModal() {
        var overlay = document.getElementById('fina-fixed-assets-modal-overlay');
        var cashOverlay = document.getElementById('fina-cash-create-modal-overlay');
        var capEffOverlay = document.getElementById('fina-cap-eff-modal-overlay');
        var navPrev = document.getElementById('fina-fixed-assets-nav-prev');
        var navNext = document.getElementById('fina-fixed-assets-nav-next');
        var growthCfgBtn = document.getElementById('fina-fixed-assets-growth-config-btn');
        var growthCfgPanel = document.getElementById('fina-fixed-assets-growth-config-panel');
        if (navPrev && !navPrev.dataset.boundNav) {
            navPrev.dataset.boundNav = '1';
            navPrev.addEventListener('click', function () {
                navigateFinaFixedAssetsModalInList(-1);
            });
        }
        if (navNext && !navNext.dataset.boundNav) {
            navNext.dataset.boundNav = '1';
            navNext.addEventListener('click', function () {
                navigateFinaFixedAssetsModalInList(1);
            });
        }
        if (growthCfgBtn && !growthCfgBtn.dataset.boundGrowthConfig) {
            growthCfgBtn.dataset.boundGrowthConfig = '1';
            growthCfgBtn.addEventListener('click', function (ev) {
                if (ev && ev.stopPropagation) ev.stopPropagation();
                var panel = document.getElementById('fina-fixed-assets-growth-config-panel');
                setFinaFixedAssetsGrowthConfigPanelOpen(!panel || panel.hidden);
            });
        }
        if (growthCfgPanel && !growthCfgPanel.dataset.boundGrowthConfigPanel) {
            growthCfgPanel.dataset.boundGrowthConfigPanel = '1';
            growthCfgPanel.addEventListener('click', function (ev) {
                if (ev && ev.stopPropagation) ev.stopPropagation();
            });
            growthCfgPanel.addEventListener('change', function (ev) {
                var target = ev && ev.target;
                if (!target || !target.getAttribute || !target.getAttribute('data-growth-key')) return;
                applyFinaFixedAssetsGrowthConfigFromPanel();
            });
        }
        var closeBtn = document.getElementById('fina-fixed-assets-modal-close');
        if (closeBtn && !closeBtn.dataset.boundClose) {
            closeBtn.dataset.boundClose = '1';
            closeBtn.addEventListener('click', closeFinaFixedAssetsModal);
        }
        if (overlay && !overlay.dataset.boundOverlay) {
            overlay.dataset.boundOverlay = '1';
            overlay.addEventListener('click', function (e) {
                if (e.target === overlay) closeFinaFixedAssetsModal();
            });
        }
        if (!document.body.dataset.finaFixedAssetsEscBound) {
            document.body.dataset.finaFixedAssetsEscBound = '1';
            document.addEventListener('keydown', function (ev) {
                if (!ev || ev.key !== 'Escape') return;
                var cashOvNow = document.getElementById('fina-cash-create-modal-overlay');
                if (cashOvNow && cashOvNow.classList.contains('is-open')) {
                    closeFinaCashCreateModal();
                    return;
                }
                var wcOvNow = document.getElementById('fina-working-cap-modal-overlay');
                if (wcOvNow && wcOvNow.classList.contains('is-open')) {
                    closeFinaWorkingCapModal();
                    return;
                }
                var ceOvNow = document.getElementById('fina-cap-eff-modal-overlay');
                if (ceOvNow && ceOvNow.classList.contains('is-open')) {
                    closeFinaCapEffModal();
                    return;
                }
                setFinaFixedAssetsGrowthConfigPanelOpen(false);
                if (!overlay || !overlay.classList.contains('is-open')) return;
                closeFinaFixedAssetsModal();
            });
        }
        var cashClose = document.getElementById('fina-cash-create-modal-close');
        if (cashClose && !cashClose.dataset.boundCashClose) {
            cashClose.dataset.boundCashClose = '1';
            cashClose.addEventListener('click', closeFinaCashCreateModal);
        }
        if (cashOverlay && !cashOverlay.dataset.boundCashOverlay) {
            cashOverlay.dataset.boundCashOverlay = '1';
            cashOverlay.addEventListener('click', function (e) {
                if (e.target === cashOverlay) closeFinaCashCreateModal();
            });
        }
        var wcOverlay = document.getElementById('fina-working-cap-modal-overlay');
        var wcClose = document.getElementById('fina-working-cap-modal-close');
        if (wcClose && !wcClose.dataset.boundWorkingCapClose) {
            wcClose.dataset.boundWorkingCapClose = '1';
            wcClose.addEventListener('click', closeFinaWorkingCapModal);
        }
        if (wcOverlay && !wcOverlay.dataset.boundWorkingCapOverlay) {
            wcOverlay.dataset.boundWorkingCapOverlay = '1';
            wcOverlay.addEventListener('click', function (e) {
                if (e.target === wcOverlay) closeFinaWorkingCapModal();
            });
        }
        var capEffClose = document.getElementById('fina-cap-eff-modal-close');
        if (capEffClose && !capEffClose.dataset.boundCapEffClose) {
            capEffClose.dataset.boundCapEffClose = '1';
            capEffClose.addEventListener('click', closeFinaCapEffModal);
        }
        if (capEffOverlay && !capEffOverlay.dataset.boundCapEffOverlay) {
            capEffOverlay.dataset.boundCapEffOverlay = '1';
            capEffOverlay.addEventListener('click', function (e) {
                if (e.target === capEffOverlay) closeFinaCapEffModal();
            });
        }
        var opDataWrap = document.getElementById('fina-fixed-assets-operating-data-wrap');
        if (opDataWrap && !opDataWrap.dataset.boundCashCreateClick) {
            opDataWrap.dataset.boundCashCreateClick = '1';
            opDataWrap.addEventListener('click', function (ev) {
                var t = ev.target;
                if (!t || !t.closest) return;
                if (t.closest('.fina-fa-opdata-cash-merge')) {
                    ev.preventDefault();
                    openFinaCashCreateModal();
                    return;
                }
                if (t.closest('.fina-fa-opdata-workingcap-merge')) {
                    ev.preventDefault();
                    openFinaWorkingCapModal();
                    return;
                }
                if (t.closest('.fina-fa-opdata-capeff-merge')) {
                    ev.preventDefault();
                    openFinaCapEffModal();
                }
            });
        }
        /* 捕获阶段委托：避免仅 6 位补后缀逻辑更新后旧缓存脚本仍绑在过时节点上；与 input 解耦 */
        if (!document.body.dataset.finaFixedAssetsTsEnterDelegated) {
            document.body.dataset.finaFixedAssetsTsEnterDelegated = '1';
            document.addEventListener(
                'keydown',
                function (ev) {
                    if (!ev || ev.key !== 'Enter') return;
                    var el = ev.target;
                    if (!el || el.id !== 'fina-fixed-assets-ts-input') return;
                    ev.preventDefault();
                    fetchFinaFixedAssetsSeries(el.value);
                },
                true
            );
        }
        var faModal = overlay && overlay.querySelector('.fina-fixed-assets-modal-shell');
        var faHeader = overlay && overlay.querySelector('.fina-fixed-assets-modal-header');
        if (faModal && faHeader && overlay.dataset.finaFixedAssetsLayoutBound !== '1') {
            overlay.dataset.finaFixedAssetsLayoutBound = '1';
            faModal.style.position = 'fixed';
            faModal.style.left = '50%';
            faModal.style.top = '50%';
            faModal.style.transform = 'translate(-50%, -50%)';
            var faDragging = false;
            var faStartX = 0;
            var faStartY = 0;
            var faStartLeft = 0;
            var faStartTop = 0;
            var faDragW = 0;
            var faDragH = 0;
            faHeader.style.cursor = 'move';
            faHeader.addEventListener('mousedown', function (e) {
                if (e.button !== 0) return;
                var t = e.target;
                if (t && t.closest && t.closest('input, button, label, select, textarea, a')) return;
                faDragging = true;
                faStartX = e.clientX;
                faStartY = e.clientY;
                var rect = faModal.getBoundingClientRect();
                faStartLeft = rect.left;
                faStartTop = rect.top;
                faDragW = rect.width;
                faDragH = rect.height;
                faModal.style.transform = '';
                e.preventDefault();
            });
            document.addEventListener('mousemove', function (e) {
                if (!faDragging) return;
                var dx = e.clientX - faStartX;
                var dy = e.clientY - faStartY;
                var newLeft = faStartLeft + dx;
                var newTop = faStartTop + dy;
                var vw = window.innerWidth || document.documentElement.clientWidth;
                var vh = window.innerHeight || document.documentElement.clientHeight;
                var maxLeft = vw - faDragW;
                var maxTop = vh - faDragH;
                if (newLeft < 0) newLeft = 0;
                if (newTop < 0) newTop = 0;
                if (newLeft > maxLeft) newLeft = maxLeft;
                if (newTop > maxTop) newTop = maxTop;
                faModal.style.left = newLeft + 'px';
                faModal.style.top = newTop + 'px';
            });
            document.addEventListener('mouseup', function () {
                if (faDragging) saveFinaFixedAssetsModalLayout(faModal);
                faDragging = false;
            });
            var faChartWrap = document.getElementById('fina-fixed-assets-chart-wrap');
            if (typeof ResizeObserver !== 'undefined') {
                var roFaChart = new ResizeObserver(function () {
                    if (finaFixedAssetsChartInstance && typeof finaFixedAssetsChartInstance.resize === 'function') {
                        try {
                            finaFixedAssetsChartInstance.resize();
                        } catch (eRo) {
                            /* ignore */
                        }
                    }
                });
                if (faChartWrap) roFaChart.observe(faChartWrap);
                var cashChartWrap = document.getElementById('fina-cash-create-chart-wrap');
                var roCashChart = new ResizeObserver(function () {
                    if (finaCashCreateChartInstance && typeof finaCashCreateChartInstance.resize === 'function') {
                        try {
                            finaCashCreateChartInstance.resize();
                        } catch (eCashRo) {
                            /* ignore */
                        }
                    }
                });
                if (cashChartWrap) roCashChart.observe(cashChartWrap);
                var wcChartWrap = document.getElementById('fina-working-cap-chart-wrap');
                var roWcChart = new ResizeObserver(function () {
                    if (finaWorkingCapTurnoverChartInstance && typeof finaWorkingCapTurnoverChartInstance.resize === 'function') {
                        try { finaWorkingCapTurnoverChartInstance.resize(); } catch (eWcRo) { /* ignore */ }
                    }
                });
                if (wcChartWrap) roWcChart.observe(wcChartWrap);
                var ceChartWrap = document.getElementById('fina-cap-eff-chart-wrap');
                var roCeChart = new ResizeObserver(function () {
                    if (finaCapEffChartInstance && typeof finaCapEffChartInstance.resize === 'function') {
                        try { finaCapEffChartInstance.resize(); } catch (eCeRo) { /* ignore */ }
                    }
                });
                if (ceChartWrap) roCeChart.observe(ceChartWrap);
            }
            if (typeof ResizeObserver !== 'undefined') {
                var roFaShell = new ResizeObserver(function () {
                    scheduleSaveFinaFixedAssetsModalLayout(faModal);
                });
                roFaShell.observe(faModal);
            }
        }
        var cashModal = cashOverlay && cashOverlay.querySelector('.fina-cash-create-modal-shell');
        var cashHeader = cashOverlay && cashOverlay.querySelector('.fina-cash-create-modal-header');
        if (cashModal && cashHeader && cashOverlay && cashOverlay.dataset.finaCashCreateLayoutBound !== '1') {
            cashOverlay.dataset.finaCashCreateLayoutBound = '1';
            cashModal.style.position = 'fixed';
            cashModal.style.left = '50%';
            cashModal.style.top = '50%';
            cashModal.style.transform = 'translate(-50%, -50%)';
            var cashDragging = false;
            var cashStartX = 0;
            var cashStartY = 0;
            var cashStartLeft = 0;
            var cashStartTop = 0;
            var cashDragW = 0;
            var cashDragH = 0;
            cashHeader.style.cursor = 'move';
            cashHeader.addEventListener('mousedown', function (e) {
                if (e.button !== 0) return;
                var t = e.target;
                if (t && t.closest && t.closest('input, button, label, select, textarea, a')) return;
                cashDragging = true;
                cashStartX = e.clientX;
                cashStartY = e.clientY;
                var rect = cashModal.getBoundingClientRect();
                cashStartLeft = rect.left;
                cashStartTop = rect.top;
                cashDragW = rect.width;
                cashDragH = rect.height;
                cashModal.style.transform = '';
                e.preventDefault();
            });
            document.addEventListener('mousemove', function (e) {
                if (!cashDragging) return;
                var dx = e.clientX - cashStartX;
                var dy = e.clientY - cashStartY;
                var newLeft = cashStartLeft + dx;
                var newTop = cashStartTop + dy;
                var vw = window.innerWidth || document.documentElement.clientWidth;
                var vh = window.innerHeight || document.documentElement.clientHeight;
                var maxLeft = vw - cashDragW;
                var maxTop = vh - cashDragH;
                if (newLeft < 0) newLeft = 0;
                if (newTop < 0) newTop = 0;
                if (newLeft > maxLeft) newLeft = maxLeft;
                if (newTop > maxTop) newTop = maxTop;
                cashModal.style.left = newLeft + 'px';
                cashModal.style.top = newTop + 'px';
            });
            document.addEventListener('mouseup', function () {
                if (cashDragging) saveFinaCashCreateModalLayout(cashModal);
                cashDragging = false;
            });
            if (typeof ResizeObserver !== 'undefined') {
                var roCashShell = new ResizeObserver(function () {
                    scheduleSaveFinaCashCreateModalLayout(cashModal);
                });
                roCashShell.observe(cashModal);
            }
        }

        var wcModal = wcOverlay && wcOverlay.querySelector('.fina-working-cap-modal-shell');
        var wcHeader = wcOverlay && wcOverlay.querySelector('.fina-working-cap-modal-header');
        if (wcModal && wcHeader && wcOverlay && wcOverlay.dataset.finaWorkingCapLayoutBound !== '1') {
            wcOverlay.dataset.finaWorkingCapLayoutBound = '1';
            wcModal.style.position = 'fixed';
            wcModal.style.left = '50%';
            wcModal.style.top = '50%';
            wcModal.style.transform = 'translate(-50%, -50%)';
            var wcDragging = false;
            var wcStartX = 0;
            var wcStartY = 0;
            var wcStartLeft = 0;
            var wcStartTop = 0;
            var wcDragW = 0;
            var wcDragH = 0;
            wcHeader.style.cursor = 'move';
            wcHeader.addEventListener('mousedown', function (e) {
                if (e.button !== 0) return;
                var t = e.target;
                if (t && t.closest && t.closest('input, button, label, select, textarea, a')) return;
                wcDragging = true;
                wcStartX = e.clientX;
                wcStartY = e.clientY;
                var rect = wcModal.getBoundingClientRect();
                wcStartLeft = rect.left;
                wcStartTop = rect.top;
                wcDragW = rect.width;
                wcDragH = rect.height;
                wcModal.style.transform = '';
                e.preventDefault();
            });
            document.addEventListener('mousemove', function (e) {
                if (!wcDragging) return;
                var dx = e.clientX - wcStartX;
                var dy = e.clientY - wcStartY;
                var newLeft = wcStartLeft + dx;
                var newTop = wcStartTop + dy;
                var vw = window.innerWidth || document.documentElement.clientWidth;
                var vh = window.innerHeight || document.documentElement.clientHeight;
                var maxLeft = vw - wcDragW;
                var maxTop = vh - wcDragH;
                if (newLeft < 0) newLeft = 0;
                if (newTop < 0) newTop = 0;
                if (newLeft > maxLeft) newLeft = maxLeft;
                if (newTop > maxTop) newTop = maxTop;
                wcModal.style.left = newLeft + 'px';
                wcModal.style.top = newTop + 'px';
            });
            document.addEventListener('mouseup', function () {
                if (wcDragging) saveFinaWorkingCapModalLayout(wcModal);
                wcDragging = false;
            });
            if (typeof ResizeObserver !== 'undefined') {
                var roWcShell = new ResizeObserver(function () {
                    scheduleSaveFinaWorkingCapModalLayout(wcModal);
                });
                roWcShell.observe(wcModal);
            }
        }
        var capEffModal = capEffOverlay && capEffOverlay.querySelector('.fina-cap-eff-modal-shell');
        var capEffHeader = capEffOverlay && capEffOverlay.querySelector('.fina-cap-eff-modal-header');
        if (capEffModal && capEffHeader && capEffOverlay && capEffOverlay.dataset.finaCapEffLayoutBound !== '1') {
            capEffOverlay.dataset.finaCapEffLayoutBound = '1';
            capEffModal.style.position = 'fixed';
            capEffModal.style.left = '50%';
            capEffModal.style.top = '50%';
            capEffModal.style.transform = 'translate(-50%, -50%)';
            var ceDragging = false;
            var ceStartX = 0;
            var ceStartY = 0;
            var ceStartLeft = 0;
            var ceStartTop = 0;
            var ceDragW = 0;
            var ceDragH = 0;
            capEffHeader.style.cursor = 'move';
            capEffHeader.addEventListener('mousedown', function (e) {
                if (e.button !== 0) return;
                var t = e.target;
                if (t && t.closest && t.closest('input, button, label, select, textarea, a')) return;
                ceDragging = true;
                ceStartX = e.clientX;
                ceStartY = e.clientY;
                var rect = capEffModal.getBoundingClientRect();
                ceStartLeft = rect.left;
                ceStartTop = rect.top;
                ceDragW = rect.width;
                ceDragH = rect.height;
                capEffModal.style.transform = '';
                e.preventDefault();
            });
            document.addEventListener('mousemove', function (e) {
                if (!ceDragging) return;
                var dx = e.clientX - ceStartX;
                var dy = e.clientY - ceStartY;
                var newLeft = ceStartLeft + dx;
                var newTop = ceStartTop + dy;
                var vw = window.innerWidth || document.documentElement.clientWidth;
                var vh = window.innerHeight || document.documentElement.clientHeight;
                var maxLeft = vw - ceDragW;
                var maxTop = vh - ceDragH;
                if (newLeft < 0) newLeft = 0;
                if (newTop < 0) newTop = 0;
                if (newLeft > maxLeft) newLeft = maxLeft;
                if (newTop > maxTop) newTop = maxTop;
                capEffModal.style.left = newLeft + 'px';
                capEffModal.style.top = newTop + 'px';
            });
            document.addEventListener('mouseup', function () {
                if (ceDragging) saveFinaCapEffModalLayout(capEffModal);
                ceDragging = false;
            });
            if (typeof ResizeObserver !== 'undefined') {
                var roCeShell = new ResizeObserver(function () {
                    scheduleSaveFinaCapEffModalLayout(capEffModal);
                });
                roCeShell.observe(capEffModal);
            }
        }
    })();
    fetchFinaMetricTable(panelKey, _finaMetricState().selectedEndDate || '');
}

function initFinaArturnPanel() {
    if (!document.getElementById('fina-arturn-section')) return;
    initFinaMetricPanel('arturn');
    initFinaArturnHorizScrollHelpers();
}
function escapeHtmlAttr(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;');
}

window.addEventListener('load', function () {
    try {
        initFinaArturnPanel();
    } catch (err) {
        logError('initFinaArturnPanel 未捕获异常', err);
        var hint = document.getElementById('fina-boot-hint');
        if (hint) {
            hint.style.display = 'block';
            hint.style.color = '#b71c1c';
            hint.textContent =
                '财报表脚本初始化失败：' + (err && err.message ? String(err.message) : String(err)) + '（请硬刷新或查看控制台）';
        }
    }
});
/* 代码更新时间：2026-04-17 12:42 固定资产：表头内联输入、移除加载钮；拖曳忽略 input */
/* 代码更新时间：2026-04-17 13:18 固定资产弹窗指标条：营收 TTM、PE、单季同比与利润率（接口同财报表口径） */
/* 代码更新时间：2026-04-17 13:22 指标条同比/净利率/ROE(扣非)负值绿字 */
/* 代码更新时间：2026-04-17 13:33 首屏 boot 提示、load 初始化 try/catch */
/* 代码更新时间：2026-04-17 13:35 固定资产指标条：当期报告期与指标同一行输出 */
/* 代码更新时间：2026-04-17 13:42 固定资产指标条文案：报告期（原「当期报告期」） */
/* 代码更新时间：2026-04-17 13:48 固定资产弹窗：按当前表顺序 上一个/下一个 */
/* 代码更新时间：2026-04-17 13:57 固定资产指标条阈值高亮与 ROE 去% */
/* 代码更新时间：2026-04-17 14:05 修复第5根柱高亮作用域并保持右起定位 */
/* 代码更新时间：2026-04-17 14:10 固定资产指标条追加申万二级/总市值/自由流通市值 */
/* 代码更新时间：2026-04-17 14:27 固定资产指标条字段值统一蓝色显示 */
/* 代码更新时间：2026-04-17 20:42 固定资产弹窗新增主营构成Top3表格渲染 */
/* 代码更新时间：2026-04-17 21:00 主营构成表扩展行业/地区（I/D）各Top3 */
/* 代码更新时间：2026-04-17 21:06 主营构成金额与占比显示去掉亿/%后缀 */
/* 代码更新时间：2026-04-17 21:17 主营构成新增毛利率%（利润/收入*100） */
/* 代码更新时间：2026-04-17 21:35 主营构成表新增报告期行（产品上方） */
/* 代码更新时间：2026-04-17 21:42 报告期并入表头，合并产品/序号两列位置 */
/* 代码更新时间：2026-04-17 14:03 单季度营收第5根柱（从右起）高亮，便于同比定位 */
/* 代码更新时间：2026-04-18 13:51 顶栏横向滚动与滚轮改为同步 document.scrollingElement，配合 CSS 去掉 wrap overflow-x:auto 以启用视口纵向 sticky */
/* 代码更新时间：2026-04-18 14:15 移除顶栏滚动条；initFinaArturnHorizScrollHelpers 仅滚轮横移 + resize 测 thead 高 */
/* 代码更新时间：2026-04-18 14:30 表头方案入口由齿轮图标改为「配置」文字（同弹窗），利于压缩第一行行高 */
/* 代码更新时间：2026-04-18 16:12 固定资产弹窗：仅 6 位代码按号段自动补 .SH/.SZ/.BJ；支持 603223SH 无点写法 */
/* 代码更新时间：2026-04-18 16:25 全角数字转 ASCII；沪 60[0-9]xxxx；Enter 用 document 捕获委托防缓存双绑 */
/* 代码更新时间：2026-04-18 16:40 normalize：已带 .SH/.SZ/.BJ 时若号段可推断则覆盖错误后缀（600000.SZ→600000.SH） */
/* 代码更新时间：2026-04-18 16:59 列筛选 popover：fixed + 触发钮 getBoundingClientRect，避免相对 section 在横滚/sticky 下漂到表左侧 */
/* 代码更新时间：2026-04-18 21:23 主营构成表：行内金额/占比/毛利率与 item_metrics 同名分项对齐（后端聚合+tot_*）；RT_MONITOR_STATIC_VER 20260418-33 */
/* 代码更新时间：2026-04-19 11:19 固定资产双轴图图例上下收紧（legend.padding + labels.padding）；RT_MONITOR_STATIC_VER 20260419-15 */
/* 代码更新时间：2026-04-19 11:23 对照季营收柱：Amber600 填充+橙900描边+对照柱数值深紫；RT_MONITOR_STATIC_VER 20260419-16 */
/* 代码更新时间：2026-04-19 11:26 固定资产图可读性：合同负债深橙红线+数值/柱顶 halo 字；RT_MONITOR_STATIC_VER 20260419-17 */
/* 代码更新时间：2026-04-19 11:29 合同负债折线底层加宽半透明描边（发光试验版）；RT_MONITOR_STATIC_VER 20260419-18 */
/* 代码更新时间：2026-04-19 11:33 固定资产图配色统一 faPal、对照季青柱、折线 Orange600+弱发光；RT_MONITOR_STATIC_VER 20260419-19 */
/* 代码更新时间：2026-04-19 11:46 固定资产弹窗第二张图（营收/现金流/经营净收益 + 三线%）；destroy+ResizeObserver；RT_MONITOR_STATIC_VER 20260419-20 */
/* 代码更新时间：2026-04-19 11:56 双图 FINA_FIXED_ASSETS_DUAL_PAL + finaFixedAssetsLegendOptions；负债线改玫红；RT_MONITOR_STATIC_VER 20260419-21 */
/* 代码更新时间：2026-04-19 11:58 固定资产两图 dataset 去「单季度」前缀（图例+Tooltip 同步）；RT_MONITOR_STATIC_VER 20260419-22 */
/* 代码更新时间：2026-04-19 第二张图下稿用数据表 renderFinaFixedAssetsOperatingDataTable；RT_MONITOR_STATIC_VER 20260419-23 */
/* 代码更新时间：2026-04-19 13:03 稿表增 q_ocf_to_or_pct 行、行极值青绿/玫红、单元格居中；RT_MONITOR_STATIC_VER 20260419-24 */
/* 代码更新时间：2026-04-19 13:05 稿表极值配色对调；文案玫红=最大·青绿=最小；RT_MONITOR_STATIC_VER 20260419-25 */
/* 代码更新时间：2026-04-19 13:10 去掉第二张组合图与稿表 caption；fetch/关闭仅 clear 经营表；RT_MONITOR_STATIC_VER 20260419-26 */
/* 代码更新时间：2026-04-19 13:16 经营稿表每行 median 着色（优先级 max>min>median）；RT_MONITOR_STATIC_VER 20260419-27 */
/* 代码更新时间：2026-04-19 13:19 经营稿表 n<0 加 fina-fa-opdata-negative；RT_MONITOR_STATIC_VER 20260419-28 */
/* 代码更新时间：2026-04-19 13:26 稿表前三行亿元 toFixed(1)；主营 fmtYi toFixed(1)；RT_MONITOR_STATIC_VER 20260419-29 */
/* 代码更新时间：2026-04-19 13:27 renderFinaFixedAssetsMetricsBar：TTM/总市值/自由市值 Arturn (,1)；RT_MONITOR_STATIC_VER 20260419-30 */
/* 代码更新时间：2026-04-19 13:28 财报表 tbody 总市值/自由市值/营收TTM formatFinaArturnValue(,1)；RT_MONITOR_STATIC_VER 20260419-31 */
/* 代码更新时间：2026-04-19 13:43 PE、应收%、p0-p3(arturn_days 四年)、弹窗 PE (,1)；RT_MONITOR_STATIC_VER 20260419-32 */
/* 代码更新时间：2026-04-19 13:46 稿表 max/min/median 并列仅最左列上色；RT_MONITOR_STATIC_VER 20260419-33 */
/* 代码更新时间：2026-04-19 15:10 稿表增扣非净利润（亿）·净利率(%)，键 q_dtprofit_yi / q_netprofit_margin；RT_MONITOR_STATIC_VER 20260419-34 */
/* 代码更新时间：2026-04-19 15:29 主营构成 P/I/D 区块序号 1 行 class mainbz-rank-1-row；RT_MONITOR_STATIC_VER 20260419-35 */
/* 代码更新时间：2026-04-19 15:34 序号1 行 class mainbz-rank-1-P|I|D（与 CSS 三色区分）；RT_MONITOR_STATIC_VER 20260419-36 */
/* 代码更新时间：2026-04-19 15:58 稿表：现金转化率（%）置底、仍用 q_ocf_to_or_pct；RT_MONITOR_STATIC_VER 20260419-37 */
/* 代码更新时间：2026-04-19 16:06 稿表 thead/tbody：季度列在前、指标列最后；RT_MONITOR_STATIC_VER 20260419-38 */
/* 代码更新时间：2026-04-19 16:20 稿表末增营业收入同比·营业利润同比(q_gr_yoy,q_op_yoy)；RT_MONITOR_STATIC_VER 20260419-39 */
/* 代码更新时间：2026-04-19 17:11 同比两列>30 浅绿底，不与 max/min/median 叠；RT_MONITOR_STATIC_VER 20260419-40 */
/* 代码更新时间：2026-04-19 17:15 同比>30 改浅蓝底（#e3f2fd）；RT_MONITOR_STATIC_VER 20260419-41 */
/* 代码更新时间：2026-04-19 17:20 ROE 行 q_dt_roe>4 浅黄 #fef3e2；RT_MONITOR_STATIC_VER 20260419-42 */
/* 代码更新时间：2026-04-19 17:27 先 band 再同比>30/ROE>4（仅 band 空）；tdNumStyled 极值类先于强调；RT_MONITOR_STATIC_VER 20260419-43 */
/* 代码更新时间：2026-04-19 18:08 ocs/oco 筛选弹窗标题去掉 fina_indicator·直接取库表值；RT_MONITOR_STATIC_VER 20260419-44 */
/* 代码更新时间：2026-04-19 20:00 配置弹窗「列表股票范围」排除 .BJ、ST；viewState 持久化；RT_MONITOR_STATIC_VER 20260419-47 */
/* 代码更新时间：2026-04-23 终极方案：DOM 一次建好永不销毁（按原始 rows 顺序全量 build + data-row-index 索引），
   筛选/排序走快路径（display 切换 + DocumentFragment 节点重排），彻底消除筛选时的 GC/layout 冻结；
   配套：markFinaMetricRowsChanged 打 _origIdx/_rowsVersion，syncRowHover 改用 dataset.rowIndex；
   筛选弹窗：per-key 历史记录（localStorage，LRU max 12）+ 列类型过滤（number/date/binary01/text）
   + 默认值策略（当前值 > 最近历史 > 空）+ 关闭浏览器 autofill；
   RT_MONITOR_STATIC_VER 20260423-47 */
/* 代码更新时间：2026-04-19 20:58 申万二级行业：英文单词 or 拆段、段间 OR 包含匹配；RT_MONITOR_STATIC_VER 20260419-49 */
/* 代码更新时间：2026-04-19 23:22 经营宽表增「股东户数」行（holder_num 整数）；RT_MONITOR_STATIC_VER 20260419-50 */
/* 代码更新时间：2026-04-19 23:30 股东户数行移至经营宽表最末行；RT_MONITOR_STATIC_VER 20260419-51 */
/* 代码更新时间：2026-04-20 10:57 经营宽表末行增「应收%」（recv_pct，与 /api 新字段一致）；RT_MONITOR_STATIC_VER 20260420-2 */
/* 代码更新时间：2026-04-20 11:29 股东户数行：holder_num/10000、decimals 2，标签「股东户数(万户)」；RT_MONITOR_STATIC_VER 20260420-3 */
/* 代码更新时间：2026-04-21 扣非右侧增单季 FCF fst0–8+fina_quarter_metrics.q_fcf_star；167 列；RT_MONITOR_STATIC_VER 20260421-3 */
/* 代码更新时间：2026-04-21 固定资产弹窗指标两行+FCF*TTM/净债务/资产负债率/CCC（API extras）；RT_MONITOR_STATIC_VER 20260421-4 */
/* 代码更新时间：2026-04-21 净现金红色 .fina-fixed-assets-metric-netcash；RT_MONITOR_STATIC_VER 20260421-5 */
/* 代码更新时间：2026-04-22 财报页流畅性：横向主导 wheel 交给原生；resize 防抖 thead 同步；拖拽弹窗 mousemove 免 getBoundingClientRect */
/* 代码更新时间：2026-04-23 07:56 固定资产弹窗顶栏标题改「速览」；RT_MONITOR_STATIC_VER 20260423-1 */
/* 代码更新时间：2026-04-23 经营宽表行序：现金转化率紧跟FCF*、ROE紧跟ROIC、应收%与股东户数互换；RT_MONITOR_STATIC_VER 20260423-2 */
/* 代码更新时间：2026-04-23 经营宽表「指标」右侧「业务逻辑」列，模块A四行 rowspan 现金创造能力；RT_MONITOR_STATIC_VER 20260423-4 */
/* 代码更新时间：2026-04-23 经营宽表前三行指标名去掉「模块A·」前缀；RT_MONITOR_STATIC_VER 20260423-5 */
/* 代码更新时间：2026-04-23 修复经营宽表函数 JSDoc 注释内含「星号+斜杠」误结束块注释、整脚本无法解析致财报页白屏；RT_MONITOR_STATIC_VER 20260423-8 */
/* 代码更新时间：2026-04-23 经营宽表 JSDoc 四行说明改为直接使用「FCF」表述；RT_MONITOR_STATIC_VER 20260423-9 */
/* 代码更新时间：2026-04-23 经营宽表现金创造能力四行 tr 分组 class + 合并格 .fina-fa-opdata-cash-merge 粗框；RT_MONITOR_STATIC_VER 20260423-10 */
/* 代码更新时间：2026-04-23 模块B/C 业务逻辑合并格（营运资本·周转天数／资本结构·杠杆）+ 黑框 tr；RT_MONITOR_STATIC_VER 20260423-11 */
/* 代码更新时间：2026-04-23 经营宽表指标列去除「模块B·」「模块C·」前缀（业务逻辑列已分区）；RT_MONITOR_STATIC_VER 20260423-12 */
/* 代码更新时间：2026-04-23 经营宽表 ROIC 行 label 改为「ROIC」；RT_MONITOR_STATIC_VER 20260423-13 */
/* 代码更新时间：2026-04-23 经营宽表业务逻辑五组（资本效率/规模.损益/盈利能力/成长性/股东与筹码）+ 应收% 行序；RT_MONITOR_STATIC_VER 20260423-14 */
/* 代码更新时间：2026-04-23 应收占收入比→模块B（应收天数后）；规模.损益改为三行；RT_MONITOR_STATIC_VER 20260423-15 */
/* 代码更新时间：2026-04-23 CCC 行指标列「现金循环天数（天）」；RT_MONITOR_STATIC_VER 20260423-16 */
/* 代码更新时间：2026-04-23 应收占收入比调至现金循环天数下一行（模块B末行）；RT_MONITOR_STATIC_VER 20260423-17 */
/* 代码更新时间：2026-04-23 经营宽表「自由现金流FCF（亿）」展示名去星号；RT_MONITOR_STATIC_VER 20260423-18 */
/* 代码更新时间：2026-04-23 经营宽表着色说明（图例）+ 指标列可读性；RT_MONITOR_STATIC_VER 20260423-19 */
/* 代码更新时间：2026-04-23 去除经营宽表顶部着色说明；RT_MONITOR_STATIC_VER 20260423-20 */
/* 代码更新时间：2026-04-25 07:41 现金创造能力合并格点击弹出 25 季 OCF/Capex/FCF + 现金转化率组合图；RT_MONITOR_STATIC_VER 20260425-1 */
/* 代码更新时间：2026-04-25 08:06 现金创造能力图增加防重叠数值标注（halo + 多档避让）；RT_MONITOR_STATIC_VER 20260425-2 */
/* 代码更新时间：2026-04-25 11:02 现金创造能力图/经营宽表：现金转化率展示名与数值均不带 %；RT_MONITOR_STATIC_VER 20260425-3 */
/* 代码更新时间：2026-04-23 筛选弹窗打开时先 hidden 测量再 rAF 聚焦，降低卡顿；公告日/上市日筛选兼容 yyyy-mm-dd；RT_MONITOR_STATIC_VER 20260423-26 */
/* 代码更新时间：2026-04-23 经营宽表应收/存货天数：相对行内均值浅红低、浅绿高；RT_MONITOR_STATIC_VER 20260423-49 */
/* 代码更新时间：2026-04-24 应收/存货保留极值三色，均值色仅补非极值格+再浅色；RT_MONITOR_STATIC_VER 20260423-50 */
/* 代码更新时间：2026-04-24 应收占收入比 recv_pct 并入 lowerIsBetterKeys + 均值行（与应收天数同）；RT_MONITOR_STATIC_VER 20260423-53 */
/* 代码更新时间：2026-04-24 应收/存货/应收占收入比：浅色偏离改相对行中位数（原算术均值）；RT_MONITOR_STATIC_VER 20260423-55 */
/* 代码更新时间：2026-04-25 13:37 现金创造能力弹窗：顶栏拖动、CSS resize、localStorage 几何；与固定资产速览弹窗同套路；RT_MONITOR_STATIC_VER 20260425-4 */
/* 代码更新时间：2026-04-25 13:45 现金创造图 FCF 柱按正负分色（负翡翠绿/正紫），与经营宽表负值字色同系；RT_MONITOR_STATIC_VER 20260425-5 */
/* 代码更新时间：2026-04-25 14:15 现金创造图 B 档：OCF 靛青负/资本深褐负/零轴加粗/转化率点 玫红|翡翠；RT_MONITOR_STATIC_VER 20260425-6 */
/* 代码更新时间：2026-04-25 14:31 固定资产弹窗指标第二行 PE 左侧增加「上市时间」（payload.list_date / arturnRow.list_date）；RT_MONITOR_STATIC_VER 20260425-7 */
/* 代码更新时间：2026-04-25 14:53 上市时间值后追加 finaListingTenureZhFromNow（已上市X年X个月）；RT_MONITOR_STATIC_VER 20260425-8 */
/* 代码更新时间：2026-04-25 15:06 营运资本周转天数二级弹窗：模块B 合并格点击，左轴4线+右轴应收占收入比柱；RT_MONITOR_STATIC_VER 20260425-9 */
/* 代码更新时间：2026-04-25 15:23 营运资本图优化：应收/应付高对比配色 + 图上数值标签 + 点样式区分；RT_MONITOR_STATIC_VER 20260425-10 */
/* 代码更新时间：2026-04-25 15:27 营运资本图优化：应收占收入比柱状图轻量化（收窄柱宽/降低透明度/右轴留白）；RT_MONITOR_STATIC_VER 20260425-11 */
/* 代码更新时间：2026-04-25 15:53 固定资产弹窗证券代码仅展示6位（formatFinaFixedAssetsTsCodeDisplay）；RT_MONITOR_STATIC_VER 20260425-12 */
/* 代码更新时间：2026-04-25 17:12 固定资产弹窗「资本效率」二级弹窗：ROIC+ROE(扣非) 25季双折线、可拖拽/resize/记忆；RT_MONITOR_STATIC_VER 20260425-13 */
/* 代码更新时间：2026-04-25 17:18 资本效率图标签策略：最早/最新报告期首尾点强制显示数值；RT_MONITOR_STATIC_VER 20260425-14 */
/* 代码更新时间：2026-04-25 17:42 资本效率图：同季度标签分槽防重叠（含最新期）+负值线段/点/标签专属配色；RT_MONITOR_STATIC_VER 20260425-15 */
/* 代码更新时间：2026-04-26 16:29 固定资产弹窗指标条：申万行业+实控人/企业性质（stock_basic.act_name、act_ent_type）；RT_MONITOR_STATIC_VER 20260426-6 */
/* 代码更新时间：2026-04-26 16:40 后端 stock_basic 直查四列（见 RT_MONITOR_STATIC_VER 20260426-7） */
/* 代码更新时间：2026-04-26 16:51 固定资产序列 API 响应头 no-store（见 RT_MONITOR_STATIC_VER 20260426-8） */
/* 代码更新时间：2026-04-26 17:05 固定资产序列请求 URL 加 _cb；成功后立即清「加载中」；RT_MONITOR_STATIC_VER 20260426-9 */
/* 代码更新时间：2026-04-26 18:05 实控人/企业性质：与序列并行拉 act 接口；财报表行 act 由后端主 SQL 与 list_date 同源；RT_MONITOR_STATIC_VER 20260426-13 */
/* 代码更新时间：2026-04-26 20:31 筛选方案弹窗：下拉选中后展示列条件/排序/保存报告期；computeFinaMetricFilterKeyTitle 与弹窗标题同源；RT_MONITOR_STATIC_VER 20260426-14 */
/* 代码更新时间：2026-04-26 21:05 computeFinaMetricFilterKeyTitle：p=应收周转天、g=销售毛利率；r/u=q_gr_yoy/q_op_yoy；fchg/kchg/invchg 带父级亿口径；未知列键可辨；RT_MONITOR_STATIC_VER 20260426-15 */
/* 代码更新时间：2026-04-26 21:27 财报表：资产负债率/带息负债率列筛选与表头分组 colspan 对齐；computeFinaMetricFilterKeyTitle 补 fqm_*；RT_MONITOR_STATIC_VER 20260426-16 */
/* 代码更新时间：2026-04-26 22:36 财报表：营业利润同比右侧增 v0–v8（利润增速−收入增速百分点）表头/行/筛选/排序/CSS 列数；RT_MONITOR_STATIC_VER 20260426-18 */
/* 代码更新时间：2026-04-26 23:05 v 列口径：仅行内 q_op_yoy−q_gr_yoy，不用 income 利润绝对额；RT_MONITOR_STATIC_VER 20260426-19 */
/* 代码更新时间：2026-04-27 08:24 冻结列「股票名称」同 f0 可点打开固定资产速览；点击委托 #fina-arturn-tbody-scroll；RT_MONITOR_STATIC_VER 20260426-20 */
/* 代码更新时间：2026-04-27 14:24 单季度毛利率分组新增「新高」「环比」两列，口径对齐后端 m_high/m_qoq；RT_MONITOR_STATIC_VER 20260427-21 */
