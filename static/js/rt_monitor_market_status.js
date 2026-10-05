/**
 * 市场状态单页：marketinfo 十三列表格 + 表头序列弹窗（可拖可缩）。
 */
(function () {
    'use strict';

    var TABLE_COL_COUNT = 38;

    var LIMIT_STORAGE_KEY = 'rt_market_status_limit_v1';
    var MODAL_LAYOUT_KEY = 'rt_market_status_chart_modal_layout_v1';
    var CHART_LINE_COLOR = 'rgba(21, 101, 192, 1)';
    var CHART_LINE_FILL = 'rgba(21, 101, 192, 0.08)';
    var CHART_HISHC_COLOR = 'rgba(183, 28, 28, 1)';
    var CHART_HISLC_COLOR = 'rgba(46, 125, 50, 1)';
    var CHART_IDX_COLOR = 'rgba(155, 89, 182, 1)';

    var metricMeta = {
        amount: { title: '成交额（亿）', label: '成交额(亿)', field: 'amount', isPct: false },
        up_pct: { title: '上涨比例（%）', label: '上涨比例(%)', field: 'up_pct', isPct: true },
        med_pct_chg: { title: '涨跌中位数（%）', label: '涨跌中位数(%)', field: 'med_pct_chg', isPct: true },
        hlhis: {
            title: 'HisHC/HisLC五日均值 vs 国证A指',
            withIdx: true,
            leftSeries: [
                {
                    field: 'hlhis_ch',
                    label: 'HisHC五日均值',
                    color: CHART_HISHC_COLOR,
                    unit: '家',
                    maDays: 5,
                },
                {
                    field: 'hlhis_cl',
                    label: 'HisLC五日均值',
                    color: CHART_HISLC_COLOR,
                    unit: '家',
                    maDays: 5,
                },
            ],
        },
        hl120_cccc: {
            title: '120DHLC五日均值 vs 国证A指',
            withIdx: true,
            leftSeries: [
                {
                    field: 'hl120_cccc',
                    label: 'HL120_CCCC五日均值',
                    color: CHART_LINE_COLOR,
                    unit: 'pp',
                    maDays: 5,
                },
            ],
        },
        hl200_cccc: {
            title: '200DHLC五日均值 vs 国证A指',
            withIdx: true,
            leftSeries: [
                {
                    field: 'hl200_cccc',
                    label: 'HL200_CCCC五日均值',
                    color: CHART_LINE_COLOR,
                    unit: 'pp',
                    maDays: 5,
                },
            ],
        },
        hl250_cccc: {
            title: '250DHLC五日均值 vs 国证A指',
            withIdx: true,
            leftSeries: [
                {
                    field: 'hl250_cccc',
                    label: 'HL250_CCCC五日均值',
                    color: CHART_LINE_COLOR,
                    unit: 'pp',
                    maDays: 5,
                },
            ],
        },
        hl850_cccc: {
            title: '850DHLC五日均值 vs 国证A指',
            withIdx: true,
            leftSeries: [
                {
                    field: 'hl850_cccc',
                    label: 'HL850_CCCC五日均值',
                    color: CHART_LINE_COLOR,
                    unit: 'pp',
                    maDays: 5,
                },
            ],
        },
        hlhis_cccc: {
            title: 'HisHLC五日均值 vs 国证A指',
            withIdx: true,
            leftSeries: [
                {
                    field: 'hlhis_cccc',
                    label: 'HLhis_CCCC五日均值',
                    color: CHART_LINE_COLOR,
                    unit: 'pp',
                    maDays: 5,
                },
            ],
        },
    };

    var currentLimit = 300;
    var currentRows = [];
    var sortDir = 'desc';
    var chartModalInstance = null;
    var modalSaveTimer = null;
    var modalLayoutOpenTs = 0;

    function rtMonitorApiUrl(path) {
        var prefix = (typeof window.__RT_MONITOR_API_PREFIX__ === 'string')
            ? window.__RT_MONITOR_API_PREFIX__
            : '';
        if (!path) return prefix || '';
        if (path.charAt(0) !== '/') path = '/' + path;
        return (prefix || '') + path;
    }

    function parseLimit(raw) {
        var n = parseInt(raw, 10);
        if (n === 0 || n === 300 || n === 1300) return n;
        return 300;
    }

    function loadStoredLimit() {
        try {
            if (typeof localStorage === 'undefined') return 300;
            return parseLimit(localStorage.getItem(LIMIT_STORAGE_KEY));
        } catch (e) {
            return 300;
        }
    }

    function saveStoredLimit(limit) {
        try {
            if (typeof localStorage !== 'undefined') {
                localStorage.setItem(LIMIT_STORAGE_KEY, String(limit));
            }
        } catch (e2) {
            /* ignore */
        }
    }

    function formatAmount(v) {
        if (v == null || isNaN(Number(v))) return '--';
        return String(Math.round(Number(v)));
    }

    function formatInt(v) {
        if (v == null || isNaN(Number(v))) return '--';
        return String(Math.round(Number(v)));
    }

    function formatDec2(v) {
        if (v == null || isNaN(Number(v))) return '--';
        return Number(v).toFixed(2);
    }

    function upPctClass(v) {
        if (v == null || isNaN(Number(v))) return '';
        return Number(v) > 50 ? 'rise' : 'fall';
    }

    function medPctChgClass(v) {
        if (v == null || isNaN(Number(v))) return '';
        var n = Number(v);
        if (n > 0) return 'rise';
        if (n < 0) return 'fall';
        return '';
    }

    /* 正值红、负值绿（5DHL% 等 HHLL 列） */
    function signClass(v) {
        if (v == null || isNaN(Number(v))) return '';
        var n = Number(v);
        if (n > 0) return 'rise';
        if (n < 0) return 'fall';
        return '';
    }

    function showMsg(text) {
        var el = document.getElementById('market-status-msg');
        if (!el) return;
        if (!text) {
            el.style.display = 'none';
            el.textContent = '';
            return;
        }
        el.textContent = text;
        el.style.display = 'block';
    }

    function updateLimitButtons() {
        var buttons = document.querySelectorAll('.market-status-limit-btn');
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var lim = parseLimit(btn.getAttribute('data-limit'));
            btn.setAttribute('aria-pressed', lim === currentLimit ? 'true' : 'false');
        }
    }

    function sortRows(rows, dir) {
        var out = rows.slice();
        out.sort(function (a, b) {
            var da = a.trade_date || '';
            var db = b.trade_date || '';
            if (da === db) return 0;
            if (dir === 'asc') return da < db ? -1 : 1;
            return da > db ? -1 : 1;
        });
        return out;
    }

    function renderTable(rows) {
        var tbody = document.getElementById('market-status-tbody');
        if (!tbody) return;
        if (!rows || rows.length === 0) {
            tbody.innerHTML = '<tr><td colspan="' + TABLE_COL_COUNT + '" class="market-status-empty">暂无数据</td></tr>';
            return;
        }
        var html = '';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var upCls = upPctClass(r.up_pct);
            var medCls = medPctChgClass(r.med_pct_chg);
            html += '<tr>';
            html += '<td class="market-status-num">' + (r.trade_date || '--') + '</td>';
            html += '<td class="market-status-num">' + formatDec2(r.total_mv_wan) + '</td>';
            html += '<td class="market-status-num">' + formatDec2(r.circ_mv_wan) + '</td>';
            html += '<td class="market-status-num">' + formatDec2(r.free_mv_wan) + '</td>';
            html += '<td class="market-status-num">' + formatAmount(r.amount) + '</td>';
            html += '<td class="market-status-num">' + formatDec2(r.amount_top5_pct) + '</td>';
            html += '<td class="market-status-num">' + formatInt(r.up_num) + '</td>';
            html += '<td class="market-status-num">' + formatInt(r.down_num) + '</td>';
            html += '<td class="market-status-num' + (upCls ? ' ' + upCls : '') + '">' + formatDec2(r.up_pct) + '</td>';
            html += '<td class="market-status-num">' + formatInt(r.total_num) + '</td>';
            html += '<td class="market-status-num' + (medCls ? ' ' + medCls : '') + '">' + formatDec2(r.med_pct_chg) + '</td>';
            html += '<td class="market-status-num">' + formatDec2(r.amplitude) + '</td>';
            html += '<td class="market-status-num">' + formatDec2(r.up_num_ma5) + '</td>';
            html += '<td class="market-status-num rise">' + formatInt(r.high_limit) + '</td>';
            html += '<td class="market-status-num rise">' + formatInt(r.high_limit_lifting) + '</td>';
            html += '<td class="market-status-num">' + formatDec2(r.high_limit_pct) + '</td>';
            html += '<td class="market-status-num fall">' + formatInt(r.low_limit) + '</td>';
            html += '<td class="market-status-num fall">' + formatInt(r.low_limit_lifting) + '</td>';
            html += '<td class="market-status-num rise">' + formatInt(r.hlhis_ch) + '</td>';
            html += '<td class="market-status-num fall">' + formatInt(r.hlhis_cl) + '</td>';
            var hlKeys = ['hl5_hhll', 'hl10_hhll', 'hl20_hhll', 'hl60_hhll', 'hl120_hhll', 'hl200_hhll', 'hl250_hhll', 'hl850_hhll', 'hlhis_hhll',
                'hl5_cccc', 'hl10_cccc', 'hl20_cccc', 'hl60_cccc', 'hl120_cccc', 'hl200_cccc', 'hl250_cccc', 'hl850_cccc', 'hlhis_cccc'];
            for (var h = 0; h < hlKeys.length; h++) {
                var hv = r[hlKeys[h]];
                var hlCls = signClass(hv);
                html += '<td class="market-status-num' + (hlCls ? ' ' + hlCls : '') + '">' + formatDec2(hv) + '</td>';
            }
            html += '</tr>';
        }
        tbody.innerHTML = html;
    }

    function loadTable(forceLimit) {
        if (forceLimit != null) currentLimit = parseLimit(forceLimit);
        saveStoredLimit(currentLimit);
        updateLimitButtons();
        var tbody = document.getElementById('market-status-tbody');
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="' + TABLE_COL_COUNT + '" class="market-status-empty">加载中…</td></tr>';
        }
        showMsg('');
        return fetch(rtMonitorApiUrl('/api/marketinfo_recent?limit=' + currentLimit), { cache: 'no-store' })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (payload) {
                if (payload.error) {
                    showMsg('[错误] ' + payload.error);
                    currentRows = [];
                    renderTable([]);
                    return;
                }
                currentRows = sortRows(payload.rows || [], sortDir);
                renderTable(currentRows);
            })
            .catch(function (err) {
                showMsg('[错误] 加载失败：' + (err && err.message ? err.message : String(err)));
                currentRows = [];
                renderTable([]);
            });
    }

    function loadModalLayout() {
        try {
            if (typeof localStorage === 'undefined') return null;
            var raw = localStorage.getItem(MODAL_LAYOUT_KEY);
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

    function saveModalLayout(modalEl) {
        try {
            if (!modalEl || typeof localStorage === 'undefined') return;
            var rect = modalEl.getBoundingClientRect();
            var w = Math.round(rect.width);
            var h = Math.round(rect.height);
            var l = Math.round(rect.left);
            var t = Math.round(rect.top);
            if (w < 320 || h < 320) return;
            localStorage.setItem(MODAL_LAYOUT_KEY, JSON.stringify({ w: w, h: h, l: l, t: t }));
        } catch (e2) {
            /* ignore */
        }
    }

    function applyModalSavedLayout(modal) {
        if (!modal) return;
        var lay = loadModalLayout();
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

    function scheduleSaveModalLayout(modalEl) {
        if (!modalEl) return;
        clearTimeout(modalSaveTimer);
        modalSaveTimer = setTimeout(function () {
            if (Date.now() - modalLayoutOpenTs < 700) return;
            saveModalLayout(modalEl);
        }, 400);
    }

    function closeChartModal() {
        var overlay = document.getElementById('market-status-chart-modal-overlay');
        var modal = overlay && overlay.querySelector('.market-status-chart-modal-shell');
        if (overlay && overlay.classList.contains('is-open') && modal) {
            saveModalLayout(modal);
        }
        if (overlay) overlay.classList.remove('is-open');
        if (chartModalInstance) {
            chartModalInstance.destroy();
            chartModalInstance = null;
        }
        var msgEl = document.getElementById('market-status-chart-modal-msg');
        if (msgEl) {
            msgEl.style.display = 'none';
            msgEl.textContent = '';
        }
    }

    function buildChartSeries(rows, field) {
        var asc = sortRows(rows, 'asc');
        var labels = [];
        var values = [];
        var fullDates = [];
        for (var i = 0; i < asc.length; i++) {
            var td = asc[i].trade_date || '';
            fullDates.push(td);
            labels.push(td.length >= 10 ? td.slice(5, 10) : String(i));
            var v = asc[i][field];
            values.push(v == null || isNaN(Number(v)) ? null : Number(v));
        }
        return { labels: labels, values: values, fullDates: fullDates, n: asc.length };
    }

    /** 左轴多序列 + 右轴 idx_close（国证A指）；leftSeries[].maDays 可选滚动均值窗口 */
    function buildIdxDualSeries(rows, leftSeries) {
        var asc = sortRows(rows, 'asc');
        var labels = [];
        var fullDates = [];
        var leftValues = [];
        var li;
        for (li = 0; li < leftSeries.length; li++) {
            leftValues.push([]);
        }
        var idxClose = [];
        for (var i = 0; i < asc.length; i++) {
            var td = asc[i].trade_date || '';
            fullDates.push(td);
            labels.push(td.length >= 10 ? td.slice(5, 10) : String(i));
            for (li = 0; li < leftSeries.length; li++) {
                var raw = asc[i][leftSeries[li].field];
                leftValues[li].push(raw == null || isNaN(Number(raw)) ? null : Number(raw));
            }
            var ix = asc[i].idx_close;
            idxClose.push(ix == null || isNaN(Number(ix)) ? null : Number(ix));
        }
        for (li = 0; li < leftSeries.length; li++) {
            var maN = parseInt(leftSeries[li].maDays, 10);
            if (!maN || maN < 2) continue;
            leftValues[li] = rollingMean(leftValues[li], maN);
        }
        return {
            labels: labels,
            leftValues: leftValues,
            idxClose: idxClose,
            fullDates: fullDates,
            n: asc.length,
        };
    }

    /** 简单滚动均值：不足窗口为 null；窗口内含 null 则结果 null */
    function rollingMean(values, windowN) {
        var out = [];
        var n = values.length;
        var w = Math.max(2, parseInt(windowN, 10) || 2);
        for (var i = 0; i < n; i++) {
            if (i + 1 < w) {
                out.push(null);
                continue;
            }
            var sum = 0;
            var ok = true;
            for (var j = i - w + 1; j <= i; j++) {
                var v = values[j];
                if (v == null || isNaN(Number(v))) {
                    ok = false;
                    break;
                }
                sum += Number(v);
            }
            out.push(ok ? Math.round((sum / w) * 100) / 100 : null);
        }
        return out;
    }

    function formatLeftAxisTooltip(v, unit) {
        if (unit === '家') return Math.round(v) + ' 家';
        if (unit === 'pp') return v.toFixed(2) + ' 百分点';
        return String(v);
    }

    function renderIdxDualChartModal(meta, canvas, msgEl, titleEl) {
        var leftSeries = meta.leftSeries || [];
        if (!leftSeries.length) return;
        var series = buildIdxDualSeries(currentRows, leftSeries);
        var n = series.n;
        if (n === 0) {
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = '暂无数据';
            }
            return;
        }
        var viewWin = currentLimit === 0 ? n : Math.min(currentLimit, n);
        var xIdxMin = n > 0 ? Math.max(0, n - viewWin) : 0;
        var xIdxMax = n > 0 ? n - 1 : 0;
        if (titleEl) {
            var winLabel = currentLimit === 0 ? '全量' : String(currentLimit);
            titleEl.textContent = meta.title + '（' + winLabel + ' 个交易日）';
        }
        var zoomOpts = {
            limits: { x: { min: 0, max: n > 0 ? n - 1 : 0, minRange: 15 } },
            pan: { enabled: true, mode: 'x', modifierKey: null, threshold: 5 },
            zoom: { wheel: { enabled: true, modifierKey: null }, pinch: { enabled: true }, mode: 'x' },
        };
        if (typeof Hammer === 'undefined') {
            zoomOpts.pan.enabled = false;
            zoomOpts.zoom.pinch.enabled = false;
        }
        var datasets = [];
        var unitByLabel = {};
        for (var li = 0; li < leftSeries.length; li++) {
            var spec = leftSeries[li];
            unitByLabel[spec.label] = spec.unit || 'pp';
            datasets.push({
                label: spec.label,
                data: series.leftValues[li],
                yAxisID: 'yHl',
                borderColor: spec.color || CHART_LINE_COLOR,
                backgroundColor: 'rgba(21, 101, 192, 0.08)',
                tension: 0.35,
                fill: false,
                pointRadius: 0,
                pointHoverRadius: 3,
                borderWidth: 1.5,
            });
        }
        datasets.push({
            label: '国证A指',
            data: series.idxClose,
            yAxisID: 'yIdx',
            borderColor: CHART_IDX_COLOR,
            backgroundColor: 'rgba(155, 89, 182, 0.08)',
            tension: 0.35,
            fill: false,
            pointRadius: 0,
            pointHoverRadius: 3,
            borderWidth: 1.5,
        });
        chartModalInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: series.labels,
                datasets: datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: 'index', intersect: false },
                layout: { padding: { bottom: 14 } },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        labels: { boxWidth: 12, boxHeight: 2, color: '#555', font: { size: 11 } },
                    },
                    zoom: zoomOpts,
                    tooltip: {
                        displayColors: true,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var idx = items[0].dataIndex;
                                var full = series.fullDates[idx];
                                return full ? ('日期：' + full) : '';
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v == null || isNaN(v)) return '';
                                var name = ctx.dataset && ctx.dataset.label ? ctx.dataset.label : '';
                                if (ctx.dataset.yAxisID === 'yIdx') {
                                    return name + '：' + Math.round(v) + ' 点';
                                }
                                return name + '：' + formatLeftAxisTooltip(v, unitByLabel[name] || 'pp');
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
                            maxRotation: 0,
                            autoSkip: true,
                            maxTicksLimit: 42,
                        },
                    },
                    yHl: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        ticks: { color: '#555' },
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
                        title: { display: false },
                        grid: { drawOnChartArea: false },
                        ticks: { color: '#7b1fa2' },
                    },
                },
            },
        });
        chartModalInstance.$marketStatusTradeDates = series.fullDates;
        requestAnimationFrame(function () {
            if (chartModalInstance && typeof chartModalInstance.resize === 'function') {
                chartModalInstance.resize();
            }
        });
    }

    function renderChartModal(metricKey) {
        var meta = metricMeta[metricKey];
        if (!meta) return;
        var canvas = document.getElementById('market-status-chart-modal-chart');
        var msgEl = document.getElementById('market-status-chart-modal-msg');
        var titleEl = document.getElementById('market-status-chart-modal-title');
        if (!canvas || !window.Chart) {
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = 'Chart.js 未加载';
            }
            return;
        }
        if (chartModalInstance) {
            chartModalInstance.destroy();
            chartModalInstance = null;
        }
        if (msgEl) {
            msgEl.style.display = 'none';
            msgEl.textContent = '';
        }
        if (meta.withIdx) {
            renderIdxDualChartModal(meta, canvas, msgEl, titleEl);
            return;
        }
        var series = buildChartSeries(currentRows, meta.field);
        var n = series.n;
        if (n === 0) {
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = '暂无数据';
            }
            return;
        }
        /* 初始视窗与顶栏窗口一致：300 / 1300 / 全量（勿固定 300） */
        var viewWin = currentLimit === 0 ? n : Math.min(currentLimit, n);
        var xIdxMin = n > 0 ? Math.max(0, n - viewWin) : 0;
        var xIdxMax = n > 0 ? n - 1 : 0;
        if (titleEl) {
            var winLabel = currentLimit === 0 ? '全量' : String(currentLimit);
            titleEl.textContent = meta.title + '（' + winLabel + ' 个交易日）';
        }
        var zoomOpts = {
            limits: { x: { min: 0, max: n > 0 ? n - 1 : 0, minRange: 15 } },
            pan: { enabled: true, mode: 'x', modifierKey: null, threshold: 5 },
            zoom: { wheel: { enabled: true, modifierKey: null }, pinch: { enabled: true }, mode: 'x' },
        };
        if (typeof Hammer === 'undefined') {
            zoomOpts.pan.enabled = false;
            zoomOpts.zoom.pinch.enabled = false;
        }
        chartModalInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: series.labels,
                datasets: [{
                    label: meta.label,
                    data: series.values,
                    borderColor: CHART_LINE_COLOR,
                    backgroundColor: CHART_LINE_FILL,
                    tension: 0.35,
                    fill: false,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    borderWidth: 1.5,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: 'index', intersect: false },
                layout: { padding: { bottom: 14 } },
                plugins: {
                    legend: { display: false },
                    zoom: zoomOpts,
                    tooltip: {
                        displayColors: false,
                        callbacks: {
                            title: function (items) {
                                if (!items || !items.length) return '';
                                var idx = items[0].dataIndex;
                                var full = series.fullDates[idx];
                                return full ? ('日期：' + full) : '';
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v == null || isNaN(v)) return '';
                                if (meta.isPct) {
                                    return meta.label.replace(/\(%\)/, '') + '：' + v.toFixed(2) + ' %';
                                }
                                return meta.label.replace(/\(亿\)/, '') + '：' + Math.round(v) + ' 亿';
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
                            maxRotation: 0,
                            autoSkip: true,
                            maxTicksLimit: 42,
                        },
                    },
                    y: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        ticks: { color: '#555' },
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
                },
            },
        });
        chartModalInstance.$marketStatusTradeDates = series.fullDates;
        requestAnimationFrame(function () {
            if (chartModalInstance && typeof chartModalInstance.resize === 'function') {
                chartModalInstance.resize();
            }
        });
    }

    function openChartModal(metricKey) {
        var overlay = document.getElementById('market-status-chart-modal-overlay');
        var modal = overlay && overlay.querySelector('.market-status-chart-modal-shell');
        if (!overlay || !modal) return;
        modalLayoutOpenTs = Date.now();
        applyModalSavedLayout(modal);
        overlay.classList.add('is-open');
        renderChartModal(metricKey);
    }

    function initChartModal() {
        var overlay = document.getElementById('market-status-chart-modal-overlay');
        var modal = overlay && overlay.querySelector('.market-status-chart-modal-shell');
        var header = overlay && overlay.querySelector('.bias-series-modal-header');
        var closeBtn = document.getElementById('market-status-chart-modal-close');
        if (closeBtn) closeBtn.addEventListener('click', closeChartModal);
        /* 透明遮罩穿透点主表，与主页超跌弹窗一致：不用点击遮罩关闭 */
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            if (overlay && overlay.classList.contains('is-open')) closeChartModal();
        });
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
                if (dragging) saveModalLayout(modal);
                dragging = false;
            });
        }
        var chartWrap = document.getElementById('market-status-chart-modal-chart-wrap');
        if (chartWrap && typeof ResizeObserver !== 'undefined') {
            var ro = new ResizeObserver(function () {
                if (chartModalInstance && typeof chartModalInstance.resize === 'function') {
                    chartModalInstance.resize();
                }
            });
            ro.observe(chartWrap);
        }
        if (modal && typeof ResizeObserver !== 'undefined') {
            var roShell = new ResizeObserver(function () {
                scheduleSaveModalLayout(modal);
            });
            roShell.observe(modal);
        }
    }

    function initTableHeaders() {
        var sortTh = document.querySelector('.market-status-th-sort');
        if (sortTh) {
            sortTh.addEventListener('click', function () {
                sortDir = sortDir === 'desc' ? 'asc' : 'desc';
                currentRows = sortRows(currentRows, sortDir);
                renderTable(currentRows);
            });
        }
        var chartThs = document.querySelectorAll('.market-status-th-chartable');
        for (var i = 0; i < chartThs.length; i++) {
            (function (th) {
                th.addEventListener('click', function () {
                    var metric = th.getAttribute('data-metric');
                    if (metric) openChartModal(metric);
                });
            })(chartThs[i]);
        }
    }

    function initToolbar() {
        var buttons = document.querySelectorAll('.market-status-limit-btn');
        for (var i = 0; i < buttons.length; i++) {
            (function (btn) {
                btn.addEventListener('click', function () {
                    var lim = parseLimit(btn.getAttribute('data-limit'));
                    loadTable(lim);
                });
            })(buttons[i]);
        }
        var refreshBtn = document.getElementById('market-status-refresh-btn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', function () {
                loadTable(currentLimit);
            });
        }
    }

    // 数据体检摘要：只读后端缓存的 latest.json，失败静默（不影响主表）
    function loadDataHealth() {
        var box = document.getElementById('market-status-health');
        if (!box || typeof fetch !== 'function') return;
        fetch(rtMonitorApiUrl('/api/data_health_latest'), { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!d || !d.available) return;
                var c = d.counts || {};
                var summary = document.getElementById('market-status-health-summary');
                summary.textContent = '数据体检 ' + d.overall + '（OK ' + (c.OK || 0) + ' / WARN ' + (c.WARN || 0)
                    + ' / FAIL ' + (c.FAIL || 0) + ' / ERROR ' + (c.ERROR || 0) + '，' + d.tables + ' 张表，'
                    + d.generatedAt + '）';
                box.setAttribute('data-overall', String(d.overall || ''));
                var list = document.getElementById('market-status-health-list');
                list.textContent = '';
                (d.attention || []).forEach(function (a) {
                    var li = document.createElement('li');
                    li.textContent = '[' + a.status + '] ' + a.table + (a.page ? '（' + a.page + '）' : '') + ' ' + (a.brief || '');
                    list.appendChild(li);
                });
                box.hidden = false;
            })
            .catch(function () { /* 体检摘要可选 */ });
    }

    function init() {
        loadDataHealth();
        currentLimit = loadStoredLimit();
        initChartModal();
        initTableHeaders();
        initToolbar();
        loadTable(currentLimit);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
/* 代码更新时间：2026-07-20 17:55 跌停开右增 HisHC/HisLC（hlhis_ch/hlhis_cl）；RT_MONITOR_STATIC_VER 20260720-1 */
/* 代码更新时间：2026-07-20 18:06 HisHC 弹窗：左轴 HisHC/HisLC + 右轴 399317.close；RT_MONITOR_STATIC_VER 20260720-2 */
/* 代码更新时间：2026-07-20 18:34 120/200/250/850/HisHLC 各自弹窗（左 HLx_CCCC + 右 399317）；RT_MONITOR_STATIC_VER 20260720-3 */
/* 代码更新时间：2026-07-20 19:13 250DHLC 弹窗左轴改为 HL250_CCCC 五日均值；RT_MONITOR_STATIC_VER 20260720-4 */
/* 代码更新时间：2026-07-20 19:19 250DHLC 弹窗左轴改为三日均值；RT_MONITOR_STATIC_VER 20260720-5 */
/* 代码更新时间：2026-07-20 19:27 250DHLC 弹窗左轴改回五日均值；RT_MONITOR_STATIC_VER 20260720-6 */
/* 代码更新时间：2026-07-20 19:35 120/200/850/HisHLC 弹窗左轴亦五日均值；RT_MONITOR_STATIC_VER 20260720-7 */
/* 代码更新时间：2026-07-20 19:47 HisHC 弹窗 HisHC/HisLC 亦五日均值；RT_MONITOR_STATIC_VER 20260720-8 */
/* 代码更新时间：2026-10-04 21:22 标题下数据体检摘要（/api/data_health_latest，失败静默）；RT_MONITOR_STATIC_VER 20261004-1 */
