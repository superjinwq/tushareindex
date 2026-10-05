# -*- coding: utf-8 -*-
"""回归：T 型顶栏 chips 不得挂在 DOMContentLoaded 上（defer 卡住会空白）。"""
from pathlib import Path


def test_opt_t_board_boots_chips_without_domcontentloaded():
    html = Path('templates/rt_monitor_opt_t_board.html').read_text(encoding='utf-8')
    assert 'function bootOptTBoard()' in html
    assert 'padding: 1px 2px 20px' in html
    assert 'padding: 1px 16px' in html.split('.opt-t-head {', 1)[1].split('.opt-t-head h1', 1)[0]
    assert 'top: 1px' in html.split('.opt-t-head {', 1)[1].split('.opt-t-head h1', 1)[0]
    assert 'align-items: center' in html.split('.opt-t-head-bar {', 1)[1].split('.opt-t-head-bar h1', 1)[0]
    assert 'align-self: center' in html.split('.opt-t-head-bar h1 {', 1)[1].split('.opt-t-chips-wrap {', 1)[0]
    assert 'bootOptTBoard();' in html
    assert "addEventListener('DOMContentLoaded'" not in html
    assert 'addEventListener("DOMContentLoaded"' not in html
    bootIdx = html.index('function bootOptTBoard()')
    bootEnd = html.index('bootOptTBoard();', bootIdx)
    chunk = html[bootIdx:bootEnd + len('bootOptTBoard();')]
    assert 'renderChips(bootU)' in chunk
    html = Path('templates/rt_monitor_opt_t_board.html').read_text(encoding='utf-8')
    percIdx = html.index('id="opt-t-perc-cfg"')
    obsIdx = html.index('id="opt-t-live-observation"')
    chipsIdx = html.index('id="opt-t-chips"')
    hintIdx = html.index('class="opt-t-hint-row"')
    addIdx = html.index('id="opt-t-add"')
    titleIdx = html.index('>期权T型报价</h1>')
    assert titleIdx < addIdx < percIdx < chipsIdx < obsIdx < hintIdx
    assert 'opt-t-chips-label">标的' not in html
    assert 'Update —' in html
    assert 'title="盘口/量仓每秒补丁' in html
    assert '字段最后变动见悬停' in html
    assert '数值未变不等于未观测' in html
    assert 'grid-template-columns: 1fr auto 1fr' in html
    assert 'flex-direction: row' in html.split('.opt-t-title-actions {', 1)[1].split('.opt-t-title-actions .opt-t-add', 1)[0]
    assert 'font-size: 0.72rem' in html.split('.opt-t-title-actions .opt-t-add {', 1)[1].split('.opt-t-chips-wrap {', 1)[0]
    assert '>添标的</button>' in html
    obsFn = html.split('function updateQuoteObservation', 1)[1].split('function quoteCellTitle', 1)[0]
    assert "el.textContent = 'Update ' + clock" in obsFn
    assert 'observed.slice(11, 19)' in obsFn
    assert '快照最后观测' not in obsFn
    assert 'opt-t-obs-line' not in html
    assert 'function loadIvDelta' in html
    assert 'OPT_T_IV_REFRESH_MS = 2000' in html
    assert "live ? OPT_T_IV_REFRESH_MS" in html
    assert 'data-opt-chip-quote' in html
    assert '.opt-t-chip-pct.is-up' in html
    assert '.opt-t-chip-pct.is-down' in html
    assert 'opt-t-chip-intra-lab' in html
    assert 'Intra：(最新−开盘)/开盘' in html
    assert 'flex-direction: column' in html.split('.opt-t-chip {', 1)[1].split('.opt-t-chip-main', 1)[0]
    activeCss = html.split('.opt-t-chip.is-active,', 1)[1].split('.opt-t-chip.is-active::after', 1)[0]
    assert 'background: #fff' in activeCss
    assert '#1565c0' not in activeCss.split('border-color', 1)[0]
    assert 'background: #1565c0' in html.split('.opt-t-chip.is-active::after', 1)[1].split('.opt-t-chip-remove', 1)[0]
    assert 'width: 7px' in html.split('.opt-t-chip.is-active::after', 1)[1].split('.opt-t-chip-remove', 1)[0]
    assert 'padding-right: 18px' in activeCss
    assert '.opt-t-chip.is-active .opt-t-chip-pct.is-up' not in html
    # 9/24 后用户确认改版：最新价独立槽位 data-opt-chip-last，四个指标色块按 Pct→Intra→Gap→Amp 排列，不再分 row1/row2
    assert 'opt-t-chip-quote-row' not in html
    assert 'data-opt-chip-last' in html
    paint = html.split('function paintChipQuote', 1)[1].split('function applyChipSpots', 1)[0]
    assert 'paintChipLast(el, spot)' in paint
    order = [paint.index(f"'{lab}',") for lab in ('Pct', 'Intra', 'Gap', 'Amp')]
    assert order == sorted(order)
    pill = html.split('function appendChipPill', 1)[1].split('function paintChipQuote', 1)[0]
    assert "pill.className = 'opt-t-chip-pct '" in pill
    assert "per.className = 'opt-t-chip-per '" in pill
    assert 'function perToneClass(' in html
    assert 'Number(per) >= 95' in html
    assert 'Number(per) <= 5' in html
    assert 'window.__optChipSpots' in html
    assert html.index('window.__optChipSpots') < html.index('bootOptTBoard();')
    assert 'applyChipSpots(window.__optChipSpots)' in chunk
    assert chunk.index('applyChipSpots(window.__optChipSpots)') < chunk.index('renderChips(bootU)')
    deltaFn = html.split('function loadQuoteDelta()', 1)[1].split('function scheduleQuoteDeltaRefresh', 1)[0]
    assert 'applyChipSpots(payload.spots)' in deltaFn
    assert deltaFn.index('applyChipSpots(payload.spots)') < deltaFn.index('seq !== quoteDeltaSeq')
    bp = Path('rt_monitor/blueprints/opt_t_board_bp.py').read_text(encoding='utf-8')
    apiFn = bp.split('def apiOptTBoard():', 1)[1].split('def apiOptTBoardQuoteDelta', 1)[0]
    assert "'spots':" in apiFn or '"spots":' in apiFn
    assert 'packChipSpotQuotes' in apiFn

    # boot 内联脚本须在 defer 静态资源之前
    inlineBoot = html.index('bootOptTBoard();')
    deferChart = html.index('chart-stack.umd.js')
    assert inlineBoot < deferChart


def test_opt_t_quote_delta_not_starved_by_board_refresh():
    """后台整表不得叠请求堵死 1s quote-delta；同标的静默重算不得作废盘口补丁。"""
    html = Path('templates/rt_monitor_opt_t_board.html').read_text(encoding='utf-8')
    lu = html.split('function loadUnderlying', 1)[1].split('function onAddClick', 1)[0]
    assert "getAttribute('data-underlying')" in lu
    assert 'prevU' in lu
    assert 'quoteDeltaSeq += 1' in lu
    assert lu.index('prevU') < lu.index('quoteDeltaSeq += 1')
    assert 'prevU !== u' in lu
    deltaFn = html.split('function loadQuoteDelta()', 1)[1].split(
        'function scheduleQuoteDeltaRefresh', 1
    )[0]
    assert "priority: 'high'" in deltaFn
    assert 'AbortController' not in deltaFn
    assert '2500' not in deltaFn
    bg = html.split('function backgroundRefreshTick', 1)[1].split(
        'function scheduleBackgroundRefresh', 1
    )[0]
    assert 'bgRefreshInflight' in bg
    assert "priority: 'low'" in bg
    assert 'AbortController' not in bg
    assert '.abort()' not in bg
    boot = html.split('function bootOptTBoard()', 1)[1].split('bootOptTBoard();', 1)[0]
    assert boot.index('loadQuoteDelta()') < boot.index('loadUnderlying(bootU')
    prefetch = html.split('function prefetchIdleChips', 1)[1].split(
        'function backgroundRefreshTick', 1
    )[0]
    assert 'prefetchUnderlying(rest.shift(), function' in prefetch
    assert 'setTimeout(next, 120)' not in prefetch


def test_opt_t_board_sellput_boots_without_chart_stack():
    html = Path('templates/rt_monitor_opt_t_board.html').read_text(encoding='utf-8')
    js = Path('static/js/rt_monitor_opt_sp_plan.js').read_text(encoding='utf-8')
    assert 'window.__optSpPlanEarly' in html
    assert 'window.__optSpPlanPendingU' in html
    assert 'window.__optSpPlanBoot' in html
    assert 'window.__optSpPlanEarly = null' in html
    assert html.index('__optSpPlanEarly') < html.index('bootOptTBoard();')
    assert html.index('rt_monitor_opt_sp_plan.js') < html.index('chart-stack.umd.js')
    assert 'function bootSpPlan()' in js
    assert 'bootSpPlan();' in js
    assert '__optSpPlanEarlyKey' in js


# 代码更新时间：2026-09-23 06:11 chip 现货与快照说明跟 Per C
# 代码更新时间：2026-09-23 07:09 chip 增 Intra 与主页 Ma/Ema 同式
# 代码更新时间：2026-09-23 07:21 顶栏距浏览器/框内上下 1px，标题垂直居中
# 代码更新时间：2026-09-23 07:53 Sell Put 首屏自启动，不得排在 chart-stack 后
# 代码更新时间：2026-09-23 07:59 壳页命中计划时跳过 head 抢跑
# 代码更新时间：2026-09-23 09:31 chip 第二行 Gap/Per
# 代码更新时间：2026-09-23 10:16 壳页 __optChipSpots，quote-delta 先涂现货
# 代码更新时间：2026-09-23 10:32 后台整表单飞，同标的静默不 bump seq
# 代码更新时间：2026-09-24 07:30 顶栏第一行 Pct、最新价、Intra
# 代码更新时间：2026-09-24 07:55 选中 chip 白底加右上角蓝点
# 代码更新时间：2026-09-24 08:20 顶栏按钮收到标题下，观测说明三行
# 代码更新时间：2026-09-24 08:34 左标题竖排、中标的、右四行观测
# 代码更新时间：2026-09-24 08:42 观测收成一行时刻，说明进悬停
# 代码更新时间：2026-09-24 08:48 添加标的与 Per C 同为 0.82rem
# 代码更新时间：2026-09-24 08:56 两按钮同一行 0.72rem，文案收成添标的
# 代码更新时间：2026-10-04 17:42 chip 断言跟随 Pct/Intra/Gap/Amp 改版
