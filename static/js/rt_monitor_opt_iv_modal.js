/**
 * opt-t-board IV「最新」弹窗：左轴 IV% + 随后 RV%，右轴 ETF 现货（spot）。
 * 范式对齐融资占比弹窗：拖标题栏、resize:both、localStorage、全样本 + 初屏最近约 300 日。
 */
(function () {
    'use strict';

    var LS_KEY = 'rt_monitor_opt_iv_series_modal_layout_v1';
    var PAGE_FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif';
    var CHART_FONT = 10;
    var IV_LINE = 'rgba(30, 64, 175, 1)';
    var RV_LINE = 'rgba(13, 148, 136, 1)';
    var SPOT_LINE = 'rgba(194, 65, 12, 1)';

    var chartInstance = null;
    var layoutOpenTs = 0;
    var resizeTimer = null;
    var chartResizeObs = null;
    var fetchAbort = null;
    var cacheByKey = Object.create(null);

    function apiPrefix() {
        var p = window.__RT_MONITOR_API_PREFIX__;
        return typeof p === 'string' ? p.replace(/\/$/, '') : '';
    }

    function apiUrl(path) {
        return apiPrefix() + path;
    }

    function loadLayout() {
        try {
            var raw = localStorage.getItem(LS_KEY);
            if (!raw) return null;
            var o = JSON.parse(raw);
            if (!o || typeof o !== 'object') return null;
            var w = parseInt(o.w, 10);
            var h = parseInt(o.h, 10);
            var l = parseInt(o.l, 10);
            var t = parseInt(o.t, 10);
            if (!(w > 200 && h > 200)) return null;
            return { w: w, h: h, l: isFinite(l) ? l : null, t: isFinite(t) ? t : null };
        } catch (e) {
            return null;
        }
    }

    function saveLayout() {
        if (Date.now() - layoutOpenTs < 700) return;
        var modal = document.querySelector('#opt-iv-series-modal-overlay .opt-iv-series-modal-shell');
        if (!modal) return;
        var r = modal.getBoundingClientRect();
        try {
            localStorage.setItem(
                LS_KEY,
                JSON.stringify({
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    l: Math.round(r.left),
                    t: Math.round(r.top),
                })
            );
        } catch (e) {}
    }

    function applySavedLayout() {
        var modal = document.querySelector('#opt-iv-series-modal-overlay .opt-iv-series-modal-shell');
        if (!modal) return;
        var lay = loadLayout();
        modal.style.position = 'fixed';
        modal.style.transform = 'none';
        if (!lay) {
            modal.style.left = '50%';
            modal.style.top = '50%';
            modal.style.transform = 'translate(-50%, -50%)';
            return;
        }
        modal.style.width = lay.w + 'px';
        modal.style.height = lay.h + 'px';
        var left = lay.l != null ? lay.l : Math.max(8, (window.innerWidth - lay.w) / 2);
        var top = lay.t != null ? lay.t : Math.max(8, (window.innerHeight - lay.h) / 2);
        left = Math.min(Math.max(0, left), Math.max(0, window.innerWidth - 80));
        top = Math.min(Math.max(0, top), Math.max(0, window.innerHeight - 80));
        modal.style.left = left + 'px';
        modal.style.top = top + 'px';
    }

    function fmtAxisIv(v) {
        if (v == null || isNaN(v)) return '';
        var n = Number(v);
        if (Math.abs(n) >= 100) return String(Math.round(n));
        return n.toFixed(1);
    }

    function fmtAxisSpot(v) {
        if (v == null || isNaN(v)) return '';
        var n = Number(v);
        if (Math.abs(n) >= 100) return String(Math.round(n));
        if (Math.abs(n) >= 10) return n.toFixed(1);
        return n.toFixed(2);
    }

    function fmtXTick(full, prev) {
        if (!full) return '';
        var s = String(full);
        var y = s.slice(0, 4);
        var md = s.slice(5, 10);
        if (!prev) return y + '\n' + md;
        var py = String(prev).slice(0, 4);
        return y !== py ? y + '\n' + md : md;
    }

    function destroyChart() {
        if (chartInstance) {
            try {
                chartInstance.destroy();
            } catch (e) {}
            chartInstance = null;
        }
    }

    function renderChart(meta, rows) {
        var canvas = document.getElementById('opt-iv-series-modal-chart');
        var msgEl = document.getElementById('opt-iv-series-modal-msg');
        if (!canvas || !window.Chart) {
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = 'Chart.js 未加载';
            }
            return;
        }
        destroyChart();
        if (msgEl) {
            msgEl.style.display = 'none';
            msgEl.textContent = '';
        }
        if (!rows || !rows.length) {
            if (msgEl) {
                msgEl.style.display = 'block';
                msgEl.textContent = '暂无序列数据';
            }
            return;
        }

        var labels = [];
        var ivArr = [];
        var rvArr = [];
        var spotArr = [];
        var dates = [];
        var ivValid = [];
        var spotValid = [];
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            dates.push(r.trade_date || '');
            labels.push(r.trade_date || '');
            var iv = r.iv == null ? null : Number(r.iv);
            var rv = r.rv == null ? null : Number(r.rv);
            var spot = r.spot == null ? null : Number(r.spot);
            ivArr.push(iv != null && isFinite(iv) ? iv : null);
            rvArr.push(rv != null && isFinite(rv) ? rv : null);
            spotArr.push(spot != null && isFinite(spot) ? spot : null);
            if (iv != null && isFinite(iv)) ivValid.push(iv);
            if (rv != null && isFinite(rv)) ivValid.push(rv);
            if (spot != null && isFinite(spot)) spotValid.push(spot);
        }

        function padRange(vals, loPad, hiPad) {
            if (!vals.length) return { min: undefined, max: undefined };
            var mn = Math.min.apply(null, vals);
            var mx = Math.max.apply(null, vals);
            var span = mx - mn;
            var pad = span > 0 ? span * 0.06 : Math.abs(mx || 1) * 0.05;
            return { min: mn - pad * (loPad || 1), max: mx + pad * (hiPad || 1) };
        }
        var ivR = padRange(ivValid);
        var spotR = padRange(spotValid);

        var zoomOpts = {
            limits: {
                x: { min: 0, max: Math.max(0, labels.length - 1), minRange: 15 },
            },
            pan: { enabled: true, mode: 'x', modifierKey: null },
            zoom: {
                wheel: { enabled: true, modifierKey: null },
                pinch: { enabled: true },
                mode: 'x',
            },
        };
        if (typeof Hammer === 'undefined') {
            zoomOpts.pan.enabled = false;
            zoomOpts.zoom.pinch.enabled = false;
        }

        chartInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: '隐含波动率',
                        data: ivArr,
                        yAxisID: 'yIv',
                        borderColor: IV_LINE,
                        backgroundColor: 'rgba(30, 64, 175, 0.08)',
                        tension: 0.2,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        borderWidth: 1.5,
                        spanGaps: false,
                    },
                    {
                        label: '随后已实现',
                        data: rvArr,
                        yAxisID: 'yIv',
                        borderColor: RV_LINE,
                        backgroundColor: 'rgba(13, 148, 136, 0.08)',
                        tension: 0.2,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        borderWidth: 1.4,
                        borderDash: [4, 3],
                        spanGaps: false,
                    },
                    {
                        label: 'ETF 价格',
                        data: spotArr,
                        yAxisID: 'ySpot',
                        borderColor: SPOT_LINE,
                        backgroundColor: 'rgba(194, 65, 12, 0.08)',
                        tension: 0.2,
                        fill: false,
                        pointRadius: 0,
                        pointHoverRadius: 3,
                        borderWidth: 1.5,
                        spanGaps: false,
                    },
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
                                var idx = items[0].dataIndex;
                                var d = chartInstance && chartInstance.$optIvDates
                                    ? chartInstance.$optIvDates[idx]
                                    : '';
                                return d ? '日期：' + d : '';
                            },
                            label: function (ctx) {
                                var v = ctx.parsed.y;
                                if (v === null || v === undefined || isNaN(v)) return '';
                                if (ctx.datasetIndex === 0) {
                                    return '隐含波动率：' + Number(v).toFixed(2) + ' %';
                                }
                                if (ctx.datasetIndex === 1) {
                                    return '随后已实现：' + Number(v).toFixed(2) + ' %';
                                }
                                return '标的价格：' + Number(v).toFixed(3);
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        type: 'category',
                        display: true,
                        grid: { display: false },
                        min: Math.max(0, labels.length - 300),
                        max: Math.max(0, labels.length - 1),
                        ticks: {
                            color: '#555',
                            font: { size: CHART_FONT, family: PAGE_FONT, lineHeight: 1.35 },
                            autoSkip: true,
                            autoSkipPadding: 2,
                            maxRotation: 0,
                            maxTicksLimit: 42,
                            callback: function (tickVal) {
                                var idx = tickVal;
                                var datesArr = this.chart.$optIvDates;
                                if (!datesArr || datesArr[idx] == null) return '';
                                var full = String(datesArr[idx]);
                                var prev =
                                    idx > 0 && datesArr[idx - 1] != null
                                        ? String(datesArr[idx - 1])
                                        : null;
                                return fmtXTick(full, prev);
                            },
                        },
                    },
                    yIv: {
                        type: 'linear',
                        position: 'left',
                        title: { display: false },
                        min: ivR.min,
                        max: ivR.max,
                        ticks: {
                            color: '#555',
                            font: { size: CHART_FONT, family: PAGE_FONT },
                            callback: function (val) {
                                return fmtAxisIv(val);
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
                    ySpot: {
                        type: 'linear',
                        position: 'right',
                        grid: { drawOnChartArea: false },
                        title: { display: false },
                        min: spotR.min,
                        max: spotR.max,
                        ticks: {
                            color: '#555',
                            font: { size: CHART_FONT, family: PAGE_FONT },
                            callback: function (val) {
                                return fmtAxisSpot(val);
                            },
                        },
                    },
                },
            },
        });
        chartInstance.$optIvDates = dates;
        chartInstance.$optIvMeta = meta;

        var ch = chartInstance;
        var n = labels.length;
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                if (!chartInstance) return;
                try {
                    chartInstance.resize();
                } catch (e1) {}
                if (n > 0 && typeof ch.zoomScale === 'function') {
                    var win = Math.min(300, n);
                    try {
                        chartInstance.zoomScale(
                            'x',
                            { min: Math.max(0, n - win), max: n - 1 },
                            'default'
                        );
                    } catch (e2) {}
                }
            });
        });
    }

    function setTitle(meta) {
        var el = document.getElementById('opt-iv-series-modal-title');
        if (!el || !meta) return;
        var n = meta.n != null ? meta.n : '';
        el.textContent =
            (meta.name || meta.code6 || '') +
            ' · ' +
            (meta.side_cn || '') +
            (meta.tenor_cn || '') +
            ' IV% · 随后RV & ETF（全样本' +
            (n !== '' ? n + '日' : '') +
            '）';
    }

    function setVrp(text) {
        var el = document.getElementById('opt-iv-series-modal-vrp');
        if (!el) return;
        var s = text ? String(text) : '';
        if (!s) {
            el.hidden = true;
            el.textContent = '';
            return;
        }
        el.hidden = false;
        el.textContent = s;
    }

    function closeModal() {
        var overlay = document.getElementById('opt-iv-series-modal-overlay');
        if (!overlay) return;
        saveLayout();
        overlay.classList.remove('is-open');
        if (fetchAbort) {
            try {
                fetchAbort.abort();
            } catch (e) {}
            fetchAbort = null;
        }
        destroyChart();
    }

    function openModal(params) {
        var overlay = document.getElementById('opt-iv-series-modal-overlay');
        var loadingEl = document.getElementById('opt-iv-series-modal-loading');
        var msgEl = document.getElementById('opt-iv-series-modal-msg');
        if (!overlay) return;
        layoutOpenTs = Date.now();
        if (msgEl) {
            msgEl.style.display = 'none';
            msgEl.textContent = '';
        }
        overlay.classList.add('is-open');
        applySavedLayout();
        destroyChart();
        setVrp('');
        setTitle({
            name: params.name,
            code6: params.u,
            side_cn: params.sideCn,
            tenor_cn: params.tenorCn,
            n: '',
        });

        var cacheKey = [params.u, params.tenor, params.cp].join('|');
        if (cacheByKey[cacheKey] && cacheByKey[cacheKey].rows) {
            if (loadingEl) loadingEl.style.display = 'none';
            setTitle(cacheByKey[cacheKey].meta);
            setVrp(cacheByKey[cacheKey].vrpText || '');
            renderChart(cacheByKey[cacheKey].meta, cacheByKey[cacheKey].rows);
            return;
        }

        if (loadingEl) loadingEl.style.display = 'flex';
        if (fetchAbort) {
            try {
                fetchAbort.abort();
            } catch (e) {}
        }
        fetchAbort = typeof AbortController !== 'undefined' ? new AbortController() : null;
        var q =
            '/api/opt-iv-series?u=' +
            encodeURIComponent(params.u) +
            '&tenor=' +
            encodeURIComponent(params.tenor) +
            '&cp=' +
            encodeURIComponent(params.cp);
        fetch(apiUrl(q), {
            signal: fetchAbort ? fetchAbort.signal : undefined,
            headers: { Accept: 'application/json' },
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (o) {
                if (loadingEl) loadingEl.style.display = 'none';
                var data = o.data || {};
                if (!o.ok || !data.ok) {
                    if (msgEl) {
                        msgEl.style.display = 'block';
                        msgEl.textContent = data.error ? String(data.error) : '加载失败';
                    }
                    return;
                }
                var meta = {
                    underlying: data.underlying,
                    code6: data.code6,
                    name: data.name,
                    tenor: data.tenor,
                    tenor_cn: data.tenor_cn,
                    call_put: data.call_put,
                    side_cn: data.side_cn,
                    n: data.n,
                };
                var rows = Array.isArray(data.rows) ? data.rows : [];
                var vrpText = data.vrp && data.vrp.text ? String(data.vrp.text) : '';
                cacheByKey[cacheKey] = { meta: meta, rows: rows, vrpText: vrpText };
                setTitle(meta);
                setVrp(vrpText);
                renderChart(meta, rows);
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return;
                if (loadingEl) loadingEl.style.display = 'none';
                if (msgEl) {
                    msgEl.style.display = 'block';
                    msgEl.textContent = '请求失败，请检查网络或服务';
                }
            });
    }

    function bindDragResize() {
        var overlay = document.getElementById('opt-iv-series-modal-overlay');
        var modal = overlay && overlay.querySelector('.opt-iv-series-modal-shell');
        var header = document.getElementById('opt-iv-series-modal-header');
        var wrap = document.getElementById('opt-iv-series-modal-chart-wrap');
        if (!modal || !header) return;

        var dragging = false;
        var startX = 0;
        var startY = 0;
        var startLeft = 0;
        var startTop = 0;
        header.style.cursor = 'move';
        header.addEventListener('mousedown', function (e) {
            if (e.button !== 0) return;
            if (e.target && e.target.closest && e.target.closest('button')) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            var r = modal.getBoundingClientRect();
            modal.style.transform = 'none';
            modal.style.left = r.left + 'px';
            modal.style.top = r.top + 'px';
            startLeft = r.left;
            startTop = r.top;
            e.preventDefault();
        });
        document.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            modal.style.left = startLeft + (e.clientX - startX) + 'px';
            modal.style.top = startTop + (e.clientY - startY) + 'px';
        });
        document.addEventListener('mouseup', function () {
            if (!dragging) return;
            dragging = false;
            saveLayout();
        });

        if (typeof ResizeObserver !== 'undefined') {
            var shellObs = new ResizeObserver(function () {
                clearTimeout(resizeTimer);
                resizeTimer = setTimeout(function () {
                    saveLayout();
                    if (chartInstance) {
                        try {
                            chartInstance.resize();
                        } catch (e) {}
                    }
                }, 400);
            });
            shellObs.observe(modal);
            if (wrap) {
                chartResizeObs = new ResizeObserver(function () {
                    if (chartInstance) {
                        try {
                            chartInstance.resize();
                        } catch (e) {}
                    }
                });
                chartResizeObs.observe(wrap);
            }
        }
    }

    function onOpenClick(el) {
        if (!el || !el.getAttribute) return;
        var u = el.getAttribute('data-u');
        var tenor = el.getAttribute('data-tenor');
        var cp = el.getAttribute('data-cp');
        if (!u || !tenor || !cp) return;
        openModal({
            u: u,
            tenor: tenor,
            cp: cp,
            name: el.getAttribute('data-name') || '',
            tenorCn: el.getAttribute('data-tenor-cn') || '',
            sideCn: el.getAttribute('data-side-cn') || '',
        });
    }

    function init() {
        var overlay = document.getElementById('opt-iv-series-modal-overlay');
        var closeBtn = document.getElementById('opt-iv-series-modal-close');
        if (closeBtn) closeBtn.addEventListener('click', closeModal);
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            if (overlay && overlay.classList.contains('is-open')) closeModal();
        });
        document.addEventListener('click', function (e) {
            var t = e.target;
            if (!t || !t.closest) return;
            var cell = t.closest('[data-opt-iv-open="1"]');
            if (cell) {
                e.preventDefault();
                onOpenClick(cell);
            }
        });
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            var t = e.target;
            if (!t || !t.getAttribute || t.getAttribute('data-opt-iv-open') !== '1') return;
            e.preventDefault();
            onOpenClick(t);
        });
        bindDragResize();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

/* 代码更新时间：2026-08-24 08:03 */
