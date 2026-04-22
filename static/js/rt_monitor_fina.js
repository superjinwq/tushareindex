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
var FINA_ARTURN_SCHEME_STORAGE_KEY = 'rt_monitor_fina_arturn_filter_schemes_v1';
var FINA_ARTURN_VIEW_STATE_STORAGE_KEY = 'rt_monitor_fina_arturn_view_state_v1';
var FINA_ARTURN_SCHEME_APPLY_PREF_KEY = 'rt_monitor_fina_arturn_scheme_apply_pref_v1';
var finaFixedAssetsChartInstance = null;
var FINA_FIXED_ASSETS_SERIES_LIMIT = 25;
/** 表头合并文案中的图表主题（与模板默认一致） */
var FINA_FIXED_ASSETS_HEADER_CHART_TITLE = '固定资产、在建工程、合同负债、季度营收';
var FINA_FIXED_ASSETS_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_fina_fixed_assets_modal_layout_v1';
var finaFixedAssetsModalLayoutOpenTs = 0;
var finaFixedAssetsModalSaveTimer = null;
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
        m0: '', m1: '', m2: '', m3: '', m4: '', m5: '', m6: '', m7: '', m8: '',
        n0: '', n1: '', n2: '', n3: '', n4: '', n5: '', n6: '', n7: '', n8: '',
        t0: '', t1: '', t2: '', t3: '', t4: '', t5: '', t6: '', t7: '', t8: '',
        kchg: '',
        k_revenue_pct: '',
        k_high: '',
        k0: '', k1: '', k2: '', k3: '', k4: '', k5: '', k6: '', k7: '', k8: '',
        fchg: '',
        f0: '', f1: '', f2: '', f3: '', f4: '', f5: '', f6: '', f7: '', f8: '',
        u0: '', u1: '', u2: '', u3: '', u4: '', u5: '', u6: '', u7: '', u8: ''
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
            excludeBj: 'fina-arturn-exclude-bj',
            excludeSt: 'fina-arturn-exclude-st'
        }
    }
};
function _finaMetricState() {
    return finaArturnState;
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

/** 与财报表 TTM 区 PE、营收 TTM 及单季列口径一致；payload 来自 /api/fina_fixed_assets_revenue_series */
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
    var rowMarket = [];
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
        '<span class="fina-fixed-assets-metric"><span class="fina-fixed-assets-metric-k">申万二级行业</span> ' +
            wrapMetricValue(sw2Esc, '') +
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
    if (wasOpen) _modalClosed();
}

function clearFinaFixedAssetsOperatingDataTable() {
    var wrap = document.getElementById('fina-fixed-assets-operating-data-wrap');
    if (wrap) wrap.innerHTML = '';
}

/**
 * 固定资产弹窗下方经营宽表：列为报告期（从左至右由旧到新，与上图 X 轴一致），「指标」行名列置于最右列；行为营收/现金流/净收益/扣非净利/毛利率/净利率/ROE/现金转化率、单季营收同比·营业利润同比、股东户数（户÷1e4 万户两位小数）；末行应收% = (balancesheet.accounts_receiv+oth_receiv)/单季营业收入×100。
 * 同比两行：>30 浅蓝、ROE>4 浅黄仅在「非该行最大/最小/中位」格叠加；该三行仍各标一枚最大(玫红)/最小(青绿)/中位(琥珀)；优先级：极值三色 > 条件浅蓝/浅黄。
 * 极值与中位数同一行内各仅标一列；若多列并列则取时间轴上最左一列（与单元格优先级 max > min > median 一致）。
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
    function tdNumStyled(rawVal, band, decPlaces, yoyOver30, roeOver4) {
        var cls = 'fina-fa-opdata-num';
        if (band === 'max') cls += ' fina-fa-opdata-cell-max';
        else if (band === 'min') cls += ' fina-fa-opdata-cell-min';
        else if (band === 'median') cls += ' fina-fa-opdata-cell-median';
        if (yoyOver30) cls += ' fina-fa-opdata-yoy-over-30';
        if (roeOver4) cls += ' fina-fa-opdata-roep-over-4';
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
        { label: '营业收入（亿）', key: 'q_revenue_yi', decimals: 1 },
        { label: '经营现金流净额（亿）', key: 'q_cashflow_yi', decimals: 1 },
        { label: '经营活动净收益（亿）', key: 'q_opincome_yi', decimals: 1 },
        { label: '扣非净利润（亿）', key: 'q_dtprofit_yi', decimals: 1 },
        { label: '毛利率(%)', key: 'q_gsprofit_margin', decimals: 2 },
        { label: '净利率(%)', key: 'q_netprofit_margin', decimals: 2 },
        { label: 'ROE(扣非)', key: 'q_dt_roe', decimals: 2 },
        { label: '现金转化率（%）', key: 'q_ocf_to_or_pct', decimals: 2 },
        { label: '营业收入同比(%)', key: 'q_gr_yoy', decimals: 2 },
        { label: '营业利润同比(%)', key: 'q_op_yoy', decimals: 2 },
        { label: '股东户数(万户)', key: 'holder_num', decimals: 2 },
        { label: '应收%', key: 'recv_pct', decimals: 2 },
    ];
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
    var bodyRows = specs
        .map(function (sp) {
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
            var medianCol = rowMedianHighlightCol(parsed);
            var cells = parsed
                .map(function (ni, colIdx) {
                    if (ni === null) {
                        var dec0 =
                            typeof sp.decimals === 'number' ? sp.decimals : 2;
                        return tdNumStyled(null, '', dec0, false, false);
                    }
                    var dec =
                        typeof sp.decimals === 'number' ? sp.decimals : 2;
                    var band = '';
                    if (finiteList.length && rowMin !== null && rowMax !== null) {
                        if (colIdx === maxCol) band = 'max';
                        else if (colIdx === minCol) band = 'min';
                        else if (medianCol >= 0 && colIdx === medianCol) band = 'median';
                    }
                    var yoyHi =
                        (sp.key === 'q_gr_yoy' || sp.key === 'q_op_yoy') &&
                        ni > 30 &&
                        band === '';
                    var roeHi = sp.key === 'q_dt_roe' && ni > 4 && band === '';
                    return tdNumStyled(ni, band, dec, yoyHi, roeHi);
                })
                .join('');
            return (
                '<tr>' +
                cells +
                '<th scope="row" class="fina-fa-opdata-rowhead">' +
                escCellText(sp.label) +
                '</th></tr>'
            );
        })
        .join('');
    wrap.innerHTML =
        '<div class="fina-fa-opdata-scroll">' +
        '<table class="fina-fa-opdata-table">' +
        '<thead><tr>' +
        headCols +
        '<th scope="col" class="fina-fa-opdata-corner">指标</th></tr></thead>' +
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
    var qRevenue = [];
    var contractLiab = [];
    for (var i = 0; i < rows.length; i++) {
        var r = rows[i] || {};
        labels.push(formatFinaQuarterLabel(r.end_date));
        var f = parseFloat(r.fix_assets_yi);
        var p = parseFloat(r.cip_yi);
        var q = parseFloat(r.q_revenue_yi);
        var c = parseFloat(r.contract_liab_yi);
        fixAssets.push(isNaN(f) ? null : f);
        cipValues.push(isNaN(p) ? null : p);
        qRevenue.push(isNaN(q) ? null : q);
        contractLiab.push(isNaN(c) ? null : c);
    }
    if (finaFixedAssetsChartInstance) {
        finaFixedAssetsChartInstance.destroy();
        finaFixedAssetsChartInstance = null;
    }
    var pal = FINA_FIXED_ASSETS_DUAL_PAL;
    var faPal = {
        fixFill: pal.fixFill,
        fixBorder: pal.fixBorder,
        revFill: pal.revenueFill,
        revBorder: pal.revenueBorder,
        refFill: pal.revenueRefFill,
        refBorder: pal.revenueRefBorder,
        cipFill: pal.cipFill,
        cipBorder: pal.cipBorder,
        lineMain: pal.contractLine,
        lineFillSoft: pal.contractFillSoft,
        lineGlow: pal.contractGlow,
        lblFix: '#1d4ed8',
        lblCip: '#b45309',
        lblRev: '#6d28d9',
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
                },
                {
                    type: 'bar',
                    label: '固定资产（亿）',
                    data: fixAssets,
                    yAxisID: 'y',
                    order: 1,
                    backgroundColor: faPal.fixFill,
                    borderColor: faPal.fixBorder,
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    type: 'bar',
                    label: '营收（亿）',
                    data: qRevenue,
                    yAxisID: 'y',
                    order: 2,
                    backgroundColor: qRevenueBgColors,
                    borderColor: qRevenueBorderColors,
                    borderWidth: qRevenueBorderWidths,
                    borderRadius: 4,
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
            layout: {
                padding: { top: 2, right: 8, bottom: 2, left: 2 },
            },
            interaction: { mode: 'index', intersect: false },
            datasets: {
                bar: {
                    categoryPercentage: 0.65,
                    barPercentage: 0.85,
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
                        font: { size: 10, family: FINA_CHART_PAGE_FONT },
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
                        font: { size: 10, family: FINA_CHART_PAGE_FONT },
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
                        font: { size: 10, family: FINA_CHART_PAGE_FONT },
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
                    /** 白描边 + 深色填字，柱线重叠区数字仍可读 */
                    function drawHaloLabel(txt, x, y, fillStyle) {
                        ctx.font = '600 11px ' + FINA_CHART_PAGE_FONT;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'bottom';
                        ctx.lineJoin = 'round';
                        ctx.lineWidth = 4;
                        ctx.strokeStyle = 'rgba(255,255,255,0.96)';
                        ctx.fillStyle = fillStyle;
                        ctx.strokeText(txt, x, y);
                        ctx.fillText(txt, x, y);
                    }
                    chart.data.datasets.forEach(function (dataset, di) {
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
                                ctx.save();
                                drawHaloLabel(
                                    v.toFixed(1),
                                    x,
                                    y - 9,
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
                                    : dataset.label === '在建工程（亿）'
                                      ? faPal.lblCip
                                      : isRefRev
                                      ? faPal.lblRef
                                      : faPal.lblRev;
                            drawHaloLabel(v.toFixed(1), x, top - 2, fill);
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
    if (inp) inp.value = normalized || raw.toUpperCase();
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
    if (inp) inp.value = normalized;
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
    var url = rtMonitorApiUrl(
        '/api/fina_fixed_assets_revenue_series?ts_code=' +
            encodeURIComponent(normalized) +
            '&end_date=' +
            encodeURIComponent(selectedEndDate) +
            '&limit=' +
            encodeURIComponent(String(FINA_FIXED_ASSETS_SERIES_LIMIT))
    );
    fetch(url, { cache: 'no-store' })
        .then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, data: data };
            });
        })
        .then(function (o) {
            if (!o.ok || !o.data || o.data.error) {
                var err = o && o.data && o.data.error ? String(o.data.error) : '加载失败';
                clearFinaFixedAssetsMetricsBar();
                clearFinaFixedAssetsMainbzTable();
                clearFinaFixedAssetsOperatingDataTable();
                showFinaFixedAssetsMsg(err);
                syncFinaFixedAssetsNavButtons();
                return;
            }
            var sn = o.data.sec_name != null ? String(o.data.sec_name) : '';
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
            renderFinaFixedAssetsMetricsBar(o.data, arturnRow);
            renderFinaFixedAssetsMainbzTable(o.data);
            var rowArr = Array.isArray(o.data.rows) ? o.data.rows : [];
            renderFinaFixedAssetsChart(rowArr, normalized);
            renderFinaFixedAssetsOperatingDataTable(rowArr);
            syncFinaFixedAssetsNavButtons();
        })
        .catch(function (err) {
            clearFinaFixedAssetsMetricsBar();
            clearFinaFixedAssetsMainbzTable();
            clearFinaFixedAssetsOperatingDataTable();
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
        inp.value = normalized || raw.toUpperCase();
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
        u8: String(s.u8 || '')
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
    var btns = document.querySelectorAll('#' + cfg.ids.thead + ' .fina-filter-btn');
    var filters = getFinaMetricFilters(panelKey);
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
    if (key === 'ar_pct' || key === 'ttm_revenue_yi' || key === 'pe_ttm' ||
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
            key === 'm0' || key === 'm1' || key === 'm2' || key === 'm3' || key === 'm4' ||
            key === 'm5' || key === 'm6' || key === 'm7' || key === 'm8' ||
            key === 'n0' || key === 'n1' || key === 'n2' || key === 'n3' || key === 'n4' ||
            key === 'n5' || key === 'n6' || key === 'n7' || key === 'n8' ||
            key === 't0' || key === 't1' || key === 't2' || key === 't3' || key === 't4' ||
            key === 't5' || key === 't6' || key === 't7' || key === 't8' ||
            key === 'kchg' ||
            key === 'k_revenue_pct' ||
            key === 'k0' || key === 'k1' || key === 'k2' || key === 'k3' ||
            key === 'k4' || key === 'k5' || key === 'k6' || key === 'k7' || key === 'k8' ||
            key === 'fchg' ||
            key === 'f0' || key === 'f1' || key === 'f2' || key === 'f3' ||
            key === 'f4' || key === 'f5' || key === 'f6' || key === 'f7' || key === 'f8' ||
            key === 'u0' || key === 'u1' || key === 'u2' || key === 'u3' || key === 'u4' ||
            key === 'u5' || key === 'u6' || key === 'u7' || key === 'u8') {
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
        key === 'm0' || key === 'm1' || key === 'm2' || key === 'm3' || key === 'm4' ||
        key === 'm5' || key === 'm6' || key === 'm7' || key === 'm8' ||
        key === 'n0' || key === 'n1' || key === 'n2' || key === 'n3' || key === 'n4' ||
        key === 'n5' || key === 'n6' || key === 'n7' || key === 'n8' ||
        key === 't0' || key === 't1' || key === 't2' || key === 't3' || key === 't4' ||
        key === 't5' || key === 't6' || key === 't7' || key === 't8' ||
        key === 'kchg' ||
        key === 'k_revenue_pct' ||
        key === 'k0' || key === 'k1' || key === 'k2' || key === 'k3' ||
        key === 'k4' || key === 'k5' || key === 'k6' || key === 'k7' || key === 'k8' ||
        key === 'fchg' ||
        key === 'f0' || key === 'f1' || key === 'f2' || key === 'f3' ||
        key === 'f4' || key === 'f5' || key === 'f6' || key === 'f7' || key === 'f8' ||
        key === 'u0' || key === 'u1' || key === 'u2' || key === 'u3' || key === 'u4' ||
        key === 'u5' || key === 'u6' || key === 'u7' || key === 'u8';
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

/** 财报宽表：横向滚动由视口根元素承担，与 thead 纵向/横向 sticky 共用同一滚动盒，避免容器 overflow-x:auto 切断吸顶 */
function finaArturnHorizScrollRoot() {
    return document.scrollingElement || document.documentElement;
}

/** 测量 thead 第一行高度，写入表 CSS 变量，供第二行 position:sticky 的 top 偏移（双行表头垂直锁定） */
function syncFinaArturnTheadStickyHeights() {
    var table = document.getElementById('fina-arturn-table');
    var thead = document.getElementById('fina-arturn-thead');
    if (!table || !thead) return;
    var r1 = thead.querySelector('tr.fina-arturn-thead-group');
    if (!r1) return;
    var h = r1.getBoundingClientRect().height;
    if (h && h > 0) {
        table.style.setProperty('--fina-arturn-thead-row1-height', Math.ceil(h) + 'px');
    }
}

/** 宽表横向仅用视口底部滚动条；绑定滚轮横向推进 + resize 时重测 thead 高度变量 */
function initFinaArturnHorizScrollHelpers() {
    var wrapEl = document.querySelector('#fina-arturn-section .fina-arturn-table-wrap');
    if (!wrapEl) return;
    if (wrapEl.dataset.finaHorizHelpers === '1') {
        syncFinaArturnTheadStickyHeights();
        return;
    }
    wrapEl.dataset.finaHorizHelpers = '1';
    /**
     * 高精度滚轮事件极密；逐次写 scrollLeft 会逼 sticky 大表反复布局。
     * 用 rAF 对齐刷新；并对单帧 applied delta 设上限，避免同一帧吃掉几百 px 造成「画面相对光标猛跳」。
     * pointerleave 时取消队列：避免手已离开表区、积压仍在下一帧写入导致「停手后还在蹭」。
     */
    var pendingHorizWheel = 0;
    var horizWheelRaf = null;
    var MAX_HORIZ_WHEEL_PER_FRAME = 420;
    /** resize 时同步 thead 高度：拖拽窗口会高频触发，防抖避免叠多次 getBoundingClientRect */
    var theadResizeTimer = null;

    function applyPendingHorizScroll() {
        horizWheelRaf = null;
        var rootEl = finaArturnHorizScrollRoot();
        var maxLeft = Math.max(0, rootEl.scrollWidth - rootEl.clientWidth);
        if (maxLeft <= 0) {
            pendingHorizWheel = 0;
            return;
        }
        if (!pendingHorizWheel) return;

        var chunk = pendingHorizWheel;
        var lim = MAX_HORIZ_WHEEL_PER_FRAME;
        if (Math.abs(chunk) > lim) {
            chunk = chunk > 0 ? lim : -lim;
            pendingHorizWheel -= chunk;
        } else {
            pendingHorizWheel = 0;
        }

        var before = rootEl.scrollLeft;
        var nextLeft = Math.max(0, Math.min(maxLeft, before + chunk));
        rootEl.scrollLeft = nextLeft;
        /* 已在左右尽头无法再吃增量：丢弃剩余 pending，避免边界上空转 rAF */
        if (nextLeft === before && chunk !== 0) {
            pendingHorizWheel = 0;
            return;
        }

        if (pendingHorizWheel !== 0) {
            horizWheelRaf = requestAnimationFrame(applyPendingHorizScroll);
        }
    }

    wrapEl.addEventListener('wheel', function (ev) {
        var dx = ev.deltaX || 0;
        var dy = ev.deltaY || 0;
        /* 触控板明确横向滑动：交给浏览器滚 document.scrollingElement（多数走合成线程），勿 preventDefault + rAF。
           Shift+纵轴仍走下方映射（业务：纵滚映射为横移）。 */
        if (!ev.shiftKey && Math.abs(dx) >= Math.abs(dy) && Math.abs(dx) > 0.5) {
            return;
        }
        var horizontalDelta = 0;
        if (Math.abs(dx) > 0) {
            horizontalDelta = dx;
        } else if (Math.abs(dy) > 0) {
            horizontalDelta = dy;
        }
        if (ev.shiftKey && Math.abs(dy) > Math.abs(dx)) {
            horizontalDelta = dy;
        }
        if (!horizontalDelta) return;

        pendingHorizWheel += horizontalDelta;
        ev.preventDefault();

        if (horizWheelRaf !== null) return;
        horizWheelRaf = requestAnimationFrame(applyPendingHorizScroll);
    }, { passive: false });

    wrapEl.addEventListener(
        'pointerleave',
        function () {
            pendingHorizWheel = 0;
            if (horizWheelRaf !== null) {
                cancelAnimationFrame(horizWheelRaf);
                horizWheelRaf = null;
            }
        },
        { passive: true }
    );
    window.addEventListener('resize', function () {
        if (theadResizeTimer) clearTimeout(theadResizeTimer);
        theadResizeTimer = setTimeout(function () {
            theadResizeTimer = null;
            syncFinaArturnTheadStickyHeights();
        }, 120);
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
    thead.innerHTML =
        '<tr class="fina-arturn-thead-group">' +
        metricHead +
        '<th colspan="1" class="fina-arturn-th-group" scope="colgroup"></th>' +
        '<th colspan="1" class="fina-arturn-th-group" scope="colgroup"></th>' +
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
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 毛利率(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 净利率(%)</th>' +
        '<th colspan="9" class="fina-arturn-th-group" scope="colgroup">单季度 ROE(扣非)</th>' +
        '<th colspan="12" class="fina-arturn-th-group" scope="colgroup">合同负债（亿）</th>' +
        '<th colspan="10" class="fina-arturn-th-group" scope="colgroup">固定资产（亿）</th>' +
        '</tr>' +
        '<tr class="fina-arturn-thead-cols">' +
        buildTh('股票代码', 'tsCode') +
        buildTh('股票名称', 'secName') +
        buildTh('申万二级行业', 'swName') +
        buildTh('总市值', 'total_mv_yi') +
        buildTh('自由市值', 'free_mv_yi') +
        buildTh('公告日', 'a0') +
        buildTh('上市日', 'listDate') +
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
    var bootHint = document.getElementById('fina-boot-hint');
    if (bootHint) bootHint.style.display = 'none';

    var filters = getFinaMetricFilters(panelKey);
    var codeKw = filters.tsCode.toLowerCase();
    var nameKw = filters.secName.toLowerCase();
    var swOrKws = finaArturnSplitSwNameOrKeywords(filters.swName);
    var ma0 = String(filters.a0 || '').trim().toLowerCase();
    var listDateKw = String(filters.listDate || '').trim().toLowerCase();
    var mTotalMv = buildNumericFilterMatcher(filters.total_mv_yi);
    var mFreeMv = buildNumericFilterMatcher(filters.free_mv_yi);
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
        if (ma0 && a0.toLowerCase().indexOf(ma0) < 0) return false;
        if (listDateKw && listDate.toLowerCase().indexOf(listDateKw) < 0) return false;
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
        tbody.innerHTML = '<tr><td colspan="167" class="median-table-cat">无匹配数据</td></tr>';
    } else {
        /* 分批渲染：首批立即显示，剩余按帧追加，防止 167 列 × N 行一次注入阻塞主线程。
           _renderToken 作为取消标记；用户切换报告期/筛选/排序时旧任务自动失效。 */
        var buildRowHtml = function (row, i) {
            var trClass = (i % 2 === 1) ? ' median-table-zebra' : '';
            var swRaw = row.sw_name != null ? String(row.sw_name) : '';
            var swTitle = swRaw ? ' title="' + escapeHtmlAttr(swRaw) + '"' : '';
            return '<tr class="median-table-row' + trClass + '">' +
                '<td class="median-table-cat">' + escapeHtmlAttr(row.ts_code || '--') + '</td>' +
                '<td class="median-table-cat">' + escapeHtmlAttr(row.sec_name || '--') + '</td>' +
                '<td class="median-table-cat fina-arturn-sw-cell"' + swTitle + '>' +
                escapeHtmlAttr(swRaw || '--') + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.total_mv_yi, 1) + '</td>' +
                '<td class="median-table-num">' + formatFinaArturnValue(row.free_mv_yi, 1) + '</td>' +
                '<td class="median-table-num">' + escapeHtmlAttr(formatFinaArturnAnnDate(row.a0)) + '</td>' +
                '<td class="median-table-num">' + escapeHtmlAttr(formatFinaArturnAnnDate(row.list_date)) + '</td>' +
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
        };
        if (st._renderChunkRaf) {
            cancelAnimationFrame(st._renderChunkRaf);
            st._renderChunkRaf = 0;
        }
        var renderToken = (st._renderToken || 0) + 1;
        st._renderToken = renderToken;
        var CHUNK_FIRST = 600;
        var CHUNK_NEXT = 1200;
        var total = filtered.length;
        var buildFinaRowsChunk = function (start, end) {
            var parts = new Array(end - start);
            for (var idxFR = start; idxFR < end; idxFR++) {
                parts[idxFR - start] = buildRowHtml(filtered[idxFR], idxFR);
            }
            return parts.join('');
        };
        var firstEnd = Math.min(CHUNK_FIRST, total);
        tbody.innerHTML = buildFinaRowsChunk(0, firstEnd);
        var renderIdx = firstEnd;
        var finaRenderStep = function () {
            if (st._renderToken !== renderToken) return;
            if (renderIdx >= total) {
                st._renderChunkRaf = 0;
                return;
            }
            var nextEnd = Math.min(renderIdx + CHUNK_NEXT, total);
            tbody.insertAdjacentHTML('beforeend', buildFinaRowsChunk(renderIdx, nextEnd));
            renderIdx = nextEnd;
            st._renderChunkRaf = requestAnimationFrame(finaRenderStep);
        };
        if (renderIdx < total) {
            st._renderChunkRaf = requestAnimationFrame(finaRenderStep);
        } else {
            st._renderChunkRaf = 0;
        }
    }

    var msgEl = document.getElementById(cfg.ids.msg);
    if (msgEl) {
        var selected = st.selectedEndDate || '--';
        msgEl.textContent = '报告期：' + selected + '；共 ' + filtered.length + ' / ' + rows.length + ' 条';
    }
    renderFinaMetricHeaderFilterButtons(panelKey);
    if (panelKey === 'arturn') {
        requestAnimationFrame(function () {
            requestAnimationFrame(syncFinaArturnTheadStickyHeights);
        });
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
    return fetch(rtMonitorApiUrl(url), { cache: 'no-store' })
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
                renderFinaMetricTable(panelKey);
                var updatedErr = document.getElementById(cfg.ids.headUpdated);
                if (updatedErr) updatedErr.textContent = '更新：--';
                if (msgEl) msgEl.textContent = '加载失败：' + String(data.error);
                saveFinaMetricViewState(panelKey);
                return { ok: false };
            }
            st.rows = Array.isArray(data.rows) ? data.rows : [];
            renderFinaMetricTable(panelKey);
            var updatedEl = document.getElementById(cfg.ids.headUpdated);
            if (updatedEl) updatedEl.textContent = '更新：' + finaArturnFormatHeadDate(data.last_update);
            saveFinaMetricViewState(panelKey);
            return { ok: true };
        })
        .catch(function (err) {
            logError(cfg.logLabel + ' 请求失败', err);
            st.rows = [];
            renderFinaMetricTable(panelKey);
            if (msgEl) msgEl.textContent = '加载失败：' + String(err && err.message ? err.message : err);
            saveFinaMetricViewState(panelKey);
            return { ok: false };
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

function openFinaMetricFilterPopover(panelKey, filterKey, btnEl) {
    var cfg = FINA_METRIC_PANEL[panelKey];
    if (!cfg) return;
    var pop = document.getElementById(cfg.ids.filterPopover);
    var titleEl = document.getElementById(cfg.ids.filterTitle);
    var inputEl = document.getElementById(cfg.ids.filterInput);
    if (!pop || !titleEl || !inputEl || !btnEl) return;
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
    } else if (!title && filterKey === 'pe_ttm') {
        title = 'PE（stk_factor_pro.pe_ttm，最新 trade_date）' + numHint;
    } else if (!title && filterKey === 'ttm_revenue_yi') {
        title = '营收·最近四单季营业总收入之和（亿元，表头不显示单位）' + numHint;
    } else if (!title && filterKey === 'ar_pct') {
        title = '应收%·TTM 单季营收' + numHint;
    } else if (!title && /^kchg$/.test(filterKey)) {
        title = '合同负债 Chg%（当期/历史8季均值-1）' + numHint;
    } else if (!title && filterKey === 'k_revenue_pct') {
        title = '营收%（当期合同负债 k0 / 营收 TTM，亿口径相除×100）' + numHint;
    } else if (!title && /^fchg$/.test(filterKey)) {
        title = '固定资产 Chg%（当期/历史8季均值-1）' + numHint;
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
    } else if (!title && filterKey === 'ocs_wqcv') {
        title = '加权离散（WQCV）：九季 ocs 的加权变异系数，最新季权重最大（衰减默认 0.88/季）；数值越小相对波动越小；有效季少于 2 为空；均值近 0 时为加权标准差' + numHint;
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
    } else if (!title && /^(p|g|c|o)[0-3]$/.test(filterKey)) {
        var idx = parseInt(filterKey.charAt(1), 10);
        var isG = filterKey.charAt(0) === 'g';
        var isC = filterKey.charAt(0) === 'c';
        var isO = filterKey.charAt(0) === 'o';
        var periods = Array.isArray(_finaMetricState().periods) ? _finaMetricState().periods : [];
        var raw = periods[idx];
        var periodLabel;
        if (raw && /^\d{8}$/.test(String(raw))) {
            periodLabel = finaArturnPeriodDisplayYear(raw);
        } else {
            var fb = ['当期', '上一年', '前两年', '前三年'];
            periodLabel = fb[idx] || filterKey;
        }
        title = (isG ? '毛利率·' : isC ? '经营现金流净额/经营活动净收益·' : isO ? '经营现金流净额/营业收入·' : '') + periodLabel + numHint;
    }
    if (!title) title = '筛选';
    var filters = getFinaMetricFilters(panelKey);
    titleEl.textContent = title;
    inputEl.value = String(filters[filterKey] || '');
    _finaMetricState().activeFilterKey = filterKey;

    /* 用视口坐标 fixed 定位：避免宽表在 document 横滚 + thead 多列 sticky 时，相对 section 的 left 与可见列错位 */
    var btnRect = btnEl.getBoundingClientRect();
    var pad = 8;
    var vw = window.innerWidth || document.documentElement.clientWidth || 0;
    var vh = window.innerHeight || document.documentElement.clientHeight || 0;
    pop.style.position = 'fixed';
    pop.style.right = 'auto';
    pop.style.bottom = 'auto';
    /* 与旧逻辑一致：弹窗左缘约在触发钮左侧 200px，便于对准列头 */
    var desiredLeft = btnRect.left - 200;
    var desiredTop = btnRect.bottom + 6;
    pop.style.left = Math.max(pad, desiredLeft) + 'px';
    pop.style.top = Math.max(pad, desiredTop) + 'px';
    pop.hidden = false;
    var popW = pop.offsetWidth || 280;
    var popH = pop.offsetHeight || 120;
    var maxLeft = Math.max(pad, vw - popW - pad);
    var leftClamped = Math.max(pad, Math.min(desiredLeft, maxLeft));
    var topClamped = Math.max(pad, Math.min(desiredTop, vh - popH - pad));
    pop.style.left = leftClamped + 'px';
    pop.style.top = topClamped + 'px';
    inputEl.focus();
    inputEl.select();
}

function openFinaFilterPopover(filterKey, btnEl) {
    openFinaMetricFilterPopover('arturn', filterKey, btnEl);
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
    var applyModeSelect = document.getElementById(ids.schemeApplyEndDateMode);
    var applyManualSelect = document.getElementById(ids.schemeApplyEndDateSelect);
    var applyRememberCheckbox = document.getElementById(ids.schemeApplyRememberPref);
    var tableHead = document.getElementById(ids.thead);
    var tbody = document.getElementById(ids.tbody);
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
            openFinaMetricFilterPopover(panelKey, key, t);
        });
    }
    if (tbody && !tbody.dataset.fixedAssetsClickBound) {
        tbody.dataset.fixedAssetsClickBound = '1';
        tbody.addEventListener('click', function (ev) {
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
            f[k] = popInput ? String(popInput.value || '').trim() : '';
            setFinaMetricFilters(panelKey, f);
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
        var navPrev = document.getElementById('fina-fixed-assets-nav-prev');
        var navNext = document.getElementById('fina-fixed-assets-nav-next');
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
                if (!overlay || !overlay.classList.contains('is-open')) return;
                closeFinaFixedAssetsModal();
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
            }
            if (typeof ResizeObserver !== 'undefined') {
                var roFaShell = new ResizeObserver(function () {
                    scheduleSaveFinaFixedAssetsModalLayout(faModal);
                });
                roFaShell.observe(faModal);
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
/* 代码更新时间：2026-04-19 20:09 财报表仅全屏遮罩二处：固定资产+筛选方案；列筛选为 #fina-arturn-filter-popover 小窗无灰底；RT_MONITOR_STATIC_VER 20260419-48 */
/* 代码更新时间：2026-04-19 20:58 申万二级行业：英文单词 or 拆段、段间 OR 包含匹配；RT_MONITOR_STATIC_VER 20260419-49 */
/* 代码更新时间：2026-04-19 23:22 经营宽表增「股东户数」行（holder_num 整数）；RT_MONITOR_STATIC_VER 20260419-50 */
/* 代码更新时间：2026-04-19 23:30 股东户数行移至经营宽表最末行；RT_MONITOR_STATIC_VER 20260419-51 */
/* 代码更新时间：2026-04-20 10:57 经营宽表末行增「应收%」（recv_pct，与 /api 新字段一致）；RT_MONITOR_STATIC_VER 20260420-2 */
/* 代码更新时间：2026-04-20 11:29 股东户数行：holder_num/10000、decimals 2，标签「股东户数(万户)」；RT_MONITOR_STATIC_VER 20260420-3 */
/* 代码更新时间：2026-04-21 扣非右侧增单季 FCF fst0–8+fina_quarter_metrics.q_fcf_star；167 列；RT_MONITOR_STATIC_VER 20260421-3 */
/* 代码更新时间：2026-04-21 固定资产弹窗指标两行+FCF*TTM/净债务/资产负债率/CCC（API extras）；RT_MONITOR_STATIC_VER 20260421-4 */
/* 代码更新时间：2026-04-21 净现金红色 .fina-fixed-assets-metric-netcash；RT_MONITOR_STATIC_VER 20260421-5 */
/* 代码更新时间：2026-04-22 财报页流畅性：横向主导 wheel 交给原生；resize 防抖 thead 同步；拖拽弹窗 mousemove 免 getBoundingClientRect */