(function () {
    var host = null;
    var lastData = null;
    var lastU = '';
    var histLayoutOpenTs = 0;
    var histResizeTimer = null;
    var histDragBound = false;
    var LS_HIST = 'rt_monitor_opt_sp_hist_modal_layout_v1';
    var fillLayoutOpenTs = 0;
    var fillResizeTimer = null;
    var fillDragBound = false;
    var LS_FILL = 'rt_monitor_opt_sp_fill_modal_layout_v1';
    var LS_CAPITAL = 'rt_monitor_opt_sp_capital_wan_v1';
    var previewTimer = null;
    var previewSeq = 0;
    var previewInflight = false;
    var planCache = {};
    var planInflight = {};
    var planSeq = 0;

    function apiPrefix() {
        var p = (window.__RT_MONITOR_API_PREFIX__ || '').replace(/\/$/, '');
        return p;
    }

    var TENOR_CN = { CM: '当月', NM: '次月', CQ: '当季', NQ: '下季' };
    var TENOR_KEY_OF = { '当月': 'CM', '次月': 'NM', '当季': 'CQ', '下季': 'NQ' };
    var fillPending = null;

    function tenorKeyOf(raw) {
        var s = String(raw == null ? '' : raw).trim();
        if (!s) return '';
        if (TENOR_CN[s]) return s;
        var head = s.split(/\s+/)[0];
        if (TENOR_CN[head]) return head;
        return TENOR_KEY_OF[s] || '';
    }

    function fmtTenor(leg) {
        if (!leg) return '—';
        var key = tenorKeyOf(leg.tenorKey) || tenorKeyOf(leg.tenor);
        var cn = TENOR_CN[key] || String(leg.tenor || '').trim();
        if (key && cn) {
            if (cn.indexOf(key) === 0) return cn;
            return key + ' ' + cn;
        }
        return key || cn || '—';
    }

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function fmtNum(v, d) {
        if (v == null || v === '') return '—';
        var n = Number(v);
        if (!isFinite(n)) return '—';
        return n.toFixed(d);
    }

    function fmtWan(v) {
        if (v == null) return '—';
        return (Number(v) / 10000).toFixed(1) + ' 万';
    }

    function clampCapitalWan(n) {
        n = Math.round(Number(n));
        if (!isFinite(n)) return 100;
        if (n < 10) return 10;
        if (n > 10000) return 10000;
        return n;
    }

    function loadCapitalWan() {
        try {
            var raw = localStorage.getItem(LS_CAPITAL);
            if (raw == null || raw === '') return 100;
            return clampCapitalWan(raw);
        } catch (e) {
            return 100;
        }
    }

    function saveCapitalWan(n) {
        try {
            localStorage.setItem(LS_CAPITAL, String(n));
        } catch (e) { /* ignore */ }
    }

    function capitalScaleOf(d) {
        var wan = d && d.capitalWan != null ? Number(d.capitalWan) : loadCapitalWan();
        if (!isFinite(wan) || wan <= 0) return 1;
        return wan / 100;
    }

    function fmtSignedWan(v, scale) {
        var n = Number(v) * (scale || 1);
        if (!isFinite(n)) return '—';
        var abs = Math.abs(n).toFixed(1);
        if (n > 0.0005) return '+' + abs + '万';
        if (n < -0.0005) return '-' + abs + '万';
        return '0万';
    }

    function fmtCalmar(v) {
        if (v == null || v === '') return '';
        var n = Number(v);
        if (!isFinite(n) || n <= 0) return '';
        return n >= 10 ? n.toFixed(0) : n.toFixed(1);
    }

    function btBits(bt, scale, kind) {
        if (!bt) return [];
        var bits = ['近一年 ' + fmtSignedWan(bt.pnlWan, scale)];
        bits.push('回撤 ' + fmtSignedWan(bt.ddWan, scale));
        var cal = fmtCalmar(bt.calmar);
        if (cal) bits.push('性价 ' + cal);
        var rnd = bt.rounds != null ? (String(bt.rounds) + '轮') : '';
        if (Number(bt.winPct) === 100 && bt.rounds != null) {
            rnd = String(bt.rounds) + '轮全胜';
        }
        if (rnd) bits.push(rnd);
        if (kind === 'combo') {
            if (bt.avgNomPct != null) bits.push('日均名义 ' + Number(bt.avgNomPct).toFixed(0) + '%');
            if (bt.peakNomPct != null) bits.push('峰值 ' + Number(bt.peakNomPct).toFixed(0) + '%');
        }
        return bits;
    }

    function btTip(bt, scale) {
        if (!bt) return '';
        var tip = [];
        if (bt.window) tip.push('窗 ' + bt.window);
        if (bt.note) tip.push(bt.note);
        if (bt.allPnlWan != null) {
            tip.push(
                '上市 ' + fmtSignedWan(bt.allPnlWan, scale) +
                ' / 回撤 ' + fmtSignedWan(bt.allDdWan, scale) +
                (bt.allRounds != null ? ' / ' + bt.allRounds + '轮' : '')
            );
        }
        return tip.join(' · ');
    }

    function btChipHtml(bt, scale, kind) {
        var bits = btBits(bt, scale, kind);
        if (!bits.length) return '';
        return '<span class="opt-sp-chip opt-sp-chip--bt" title="' + esc(btTip(bt, scale)) + '">' +
            esc(bits.join(' / ')) + '</span>';
    }

    function btInlineHtml(bt, scale) {
        var bits = btBits(bt, scale, 'combo');
        if (!bits.length) return '';
        return '<span class="opt-sp-bt-inline" title="' + esc(btTip(bt, scale)) + '">' +
            esc(bits.join(' · ')) + '</span>';
    }

    var RULE_ACCENT = {
        'Gap': 'gap',
        '动量': 'mom',
        'BIAS': 'bias'
    };

    function ruleAccentOf(name) {
        var s = String(name || '');
        if (s.indexOf('通道') >= 0) return 'ch';
        return RULE_ACCENT[s] || '';
    }

    // 计划区现货=etf_daily 末日收盘（T-1），预演区现货=盘中实时 tick；
    // 两处若同名会造成多口径歧义，故此处显式标注口径与行情日。
    function planSpotLabel(d) {
        var txt = '标的收盘 ' + fmtNum(d.spot, 3);
        var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d.asof || ''));
        if (m) txt += ' (' + m[2] + '-' + m[3] + ')';
        return txt;
    }

    function flagChips(d) {
        var f = d.flags || {};
        var tags = [];
        function push(label, kind) { tags.push({ label: label, kind: kind }); }
        push(planSpotLabel(d), 'spot');
        if (d.brkH) push('通道突破', 'sig');
        if (f.gapExt) push('Gap', 'sig');
        if (f.momOr) push(f.up120 ? '动量' : '动量(未过MA120)', 'sig');
        if (f.biasOs) push('BIAS', 'sig');
        if (f.danger) push('危险日', 'warn');
        if (f.overheat) push('过热警示', 'warn');
        if (f.holidayReopen) push('长假后首日禁开', 'warn');
        var lowHits = d.exitLowHits;
        if (lowHits && lowHits.length) {
            for (var i = 0; i < lowHits.length; i++) push(lowHits[i], 'warn');
        } else {
            if (d.exitL10) push('10日新低', 'warn');
            if (d.exitL9) push('9日新低', 'warn');
            if (d.exitL8) push('8日新低', 'warn');
            if (d.exitL7) push('7日新低', 'warn');
            if (d.exitL6) push('6日新低', 'warn');
            if (d.exitL5) push('5日新低', 'warn');
            if (d.exitL4) push('4日新低', 'warn');
        }
        if (tags.length === 1) push('无四信号', '');
        return tags.map(function (t) {
            var cls = 'opt-sp-chip';
            if (t.kind === 'spot') cls += ' opt-sp-chip--spot';
            if (t.kind === 'sig') cls += ' opt-sp-chip--sig';
            if (t.kind === 'warn') cls += ' opt-sp-chip--warn';
            return '<span class="' + cls + '">' + esc(t.label) + '</span>';
        }).join('');
    }

    function occBar(occ, cap) {
        occ = occ || {};
        cap = Number(cap);
        if (!isFinite(cap) || cap <= 0) cap = 160;
        var ch = Number(occ['通道'] || 0);
        var gap = Number(occ['Gap'] || 0);
        var mom = Number(occ['动量'] || 0);
        var bias = Number(occ['BIAS'] || 0);
        var used = ch + gap + mom + bias;
        return (
            '<div class="opt-sp-occ opt-sp-occ--inline">' +
            '<div class="opt-sp-occ-lab">名义占用 ' + used.toFixed(0) + '% / 计划合计 ' + cap.toFixed(0) + '%（无替换）</div>' +
            '<div class="opt-sp-occ-track">' +
            '<i class="opt-sp-occ-ch" style="width:' + (ch / cap * 100) + '%"></i>' +
            '<i class="opt-sp-occ-gap" style="width:' + (gap / cap * 100) + '%"></i>' +
            '<i class="opt-sp-occ-mom" style="width:' + (mom / cap * 100) + '%"></i>' +
            '<i class="opt-sp-occ-bias" style="width:' + (bias / cap * 100) + '%"></i>' +
            '</div>' +
            '<div class="opt-sp-occ-legend">' +
            '<span><i class="opt-sp-occ-ch"></i>通道 ' + ch.toFixed(0) + '%</span>' +
            '<span><i class="opt-sp-occ-gap"></i>Gap ' + gap.toFixed(0) + '%</span>' +
            '<span><i class="opt-sp-occ-mom"></i>动量 ' + mom.toFixed(0) + '%</span>' +
            '<span><i class="opt-sp-occ-bias"></i>BIAS ' + bias.toFixed(0) + '%</span>' +
            '</div></div>'
        );
    }

    function statusPills(d) {
        var nOpen = (d.opens || []).length;
        var nClose = (d.closes || []).length;
        var nHold = (d.holds || []).length;
        function pill(label, n, emptyText) {
            var body = n ? (n + '笔') : emptyText;
            return '<span class="opt-sp-chip' + (n ? ' opt-sp-chip--on' : '') + '">' +
                label + ' ' + body + '</span>';
        }
        var openEmpty = '无';
        if (!nOpen && d.holidayReopen) {
            var n = d.prevCalDays != null ? d.prevCalDays : '≥7';
            openEmpty = '长假后首日禁开（' + esc(String(n)) + '日）';
        }
        return '<div class="opt-sp-status-pills">' +
            pill('开仓', nOpen, openEmpty) +
            pill('平仓', nClose, '无') +
            pill('持仓', nHold, '空仓') +
            '</div>';
    }

    function wrapSec(title, extra, body) {
        return '<section class="opt-sp-sec"><h3 class="opt-sp-h">' + title + (extra || '') +
            '</h3>' + body + '</section>';
    }

    function fmtYm(mat) {
        var s = String(mat == null ? '' : mat);
        return s.length >= 7 ? s.slice(0, 7) : s;
    }

    function fmtContractLine(leg) {
        if (!leg) return '—';
        var cn = TENOR_CN[tenorKeyOf(leg.tenorKey) || tenorKeyOf(leg.tenor)] || String(leg.tenor || '');
        var shortCn = cn === '当月' ? '当' : (cn === '次月' ? '次' : cn);
        var ym = fmtYm(leg.mat);
        var mm = ym.length >= 7 ? ym.slice(5, 7) : ym;
        var head = shortCn ? (shortCn + (mm ? mm + '月' : '')) : (mm ? mm + '月' : '');
        if (leg.dte != null && leg.dte !== '') {
            head += (head ? '·' : '') + leg.dte + '天';
        }
        return head || '—';
    }

    function fmtContractTitle(leg) {
        if (!leg) return '';
        var cn = TENOR_CN[tenorKeyOf(leg.tenorKey) || tenorKeyOf(leg.tenor)] || String(leg.tenor || '');
        var ym = fmtYm(leg.mat);
        var bits = [];
        if (cn) bits.push(cn);
        if (ym) bits.push(ym);
        var head = bits.join(' ');
        if (leg.dte != null && leg.dte !== '') {
            head += (head ? ' · ' : '') + '剩' + leg.dte + '天';
        }
        return head;
    }

    function uniqJoin(vals, sep) {
        var seen = [];
        for (var i = 0; i < vals.length; i++) {
            if (seen.indexOf(vals[i]) < 0) seen.push(vals[i]);
        }
        return seen.join(sep);
    }

    function fmtBookSignal(r) {
        var name = String((r && r.signalName) || '');
        var book = String((r && r.book) || '');
        if (!book || name === book) return name || book || '—';
        return name + '（' + book + '）';
    }

    function wrapTable(html) {
        return '<div class="opt-sp-table-wrap">' + html + '</div>';
    }

    function openFld(lab, val, tip, extraCls) {
        return '<div class="opt-sp-fld' + (extraCls ? ' ' + extraCls : '') + '">' +
            '<dt>' + esc(lab) + '</dt>' +
            '<dd' + (tip ? ' title="' + esc(tip) + '"' : '') + '>' + esc(val) + '</dd></div>';
    }

    function openTable(d) {
        var rows = d.opens || [];
        if (!rows.length) return '';
        var html = '<div class="opt-sp-open-list">';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var legs = r.legs || [];
            var contracts = legs.length
                ? legs.map(fmtContractLine).join(' / ')
                : (r.tenorLabel || fmtTenor(r));
            var contractTip = legs.length
                ? legs.map(fmtContractTitle).join('；')
                : contracts;
            var ks = legs.length
                ? uniqJoin(legs.map(function (x) { return fmtNum(x.K, 3); }), ' / ')
                : '—';
            var lots = legs.length
                ? uniqJoin(legs.map(function (x) { return String(x.lots); }), ' / ')
                : String(r.lots == null ? '' : r.lots);
            html += '<article class="opt-sp-open-card">' +
                '<div class="opt-sp-open-id">' +
                '<span class="opt-sp-chip opt-sp-chip--sig">' + esc(fmtBookSignal(r)) + '</span>' +
                '<span class="opt-sp-open-act">卖认沽</span></div>' +
                '<dl class="opt-sp-open-fields">' +
                openFld('合约', contracts, contractTip) +
                openFld('行权价 K', ks) +
                openFld('张', lots) +
                openFld('名义本金', fmtWan(r.notional)) +
                openFld('出口', r.exitHint || r.exitProfile || '—', r.exitProfile, 'opt-sp-fld--exit') +
                '</dl>' +
                '<button type="button" class="opt-sp-btn opt-sp-btn-act" data-act="open" data-idx="' + i +
                '">确认成交</button></article>';
        }
        html += '</div>';
        return wrapSec('次日开仓', '', html);
    }

    function closeTable(d) {
        var rows = d.closes || [];
        if (!rows.length) return '';
        var html = '<table class="opt-sp-table"><thead><tr>' +
            '<th>信号</th><th>动作</th><th>合约</th><th>到期月</th><th>张</th><th class="opt-sp-th-fill">原因</th><th>入场</th><th>参考收盘</th><th class="opt-sp-th-act"></th>' +
            '</tr></thead><tbody>';
        var seen = {};
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            html += '<tr>' +
                '<td>' + esc(fmtBookSignal(r)) + '</td>' +
                '<td>买回</td>' +
                '<td>' + esc(r.tsCode) + '</td>' +
                '<td>' + esc(fmtTenor(r)) + '</td>' +
                '<td>' + esc(r.lots) + '</td>' +
                '<td class="opt-sp-td-text">' + esc(r.reason) + '</td>' +
                '<td>' + esc(fmtNum(r.entryPrem, 4)) + '</td>' +
                '<td>' + esc(fmtNum(r.refPrem, 4)) + '</td>' +
                '<td>';
            if (!seen[r.book]) {
                html += '<button type="button" class="opt-sp-btn opt-sp-btn-act" data-act="correct" data-book="' +
                    esc(r.book) + '">更正</button> ';
                html += '<button type="button" class="opt-sp-btn opt-sp-btn-act" data-act="close" data-book="' +
                    esc(r.book) + '">确认成交</button>';
                seen[r.book] = 1;
            }
            html += '</td></tr>';
        }
        html += '</tbody></table>';
        return wrapSec('次日平仓', '', wrapTable(html));
    }

    function holdTable(d) {
        var rows = d.holds || [];
        if (!rows.length) return '';
        var html = '<table class="opt-sp-table"><thead><tr>' +
            '<th>信号</th><th>账本</th><th>开仓日</th><th>到期月</th><th>K</th><th>张</th><th>入场</th><th>现收盘</th><th>已持有</th><th class="opt-sp-th-fill">盯什么</th><th class="opt-sp-th-act"></th>' +
            '</tr></thead><tbody>';
        var seen = {};
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            html += '<tr>' +
                '<td>' + esc(r.signalName) + '</td>' +
                '<td>' + esc(r.book) + '</td>' +
                '<td>' + esc(r.entryDate) + '</td>' +
                '<td>' + esc(fmtTenor(r)) + '</td>' +
                '<td>' + esc(fmtNum(r.K, 3)) + '</td>' +
                '<td>' + esc(r.lots) + '</td>' +
                '<td>' + esc(fmtNum(r.entryPrem, 4)) + '</td>' +
                '<td>' + esc(fmtNum(r.refPrem, 4)) + '</td>' +
                '<td>' + esc(r.holdDays) + ' 日</td>' +
                '<td class="opt-sp-td-text">' + esc(r.watch) + '</td>' +
                '<td class="opt-sp-td-act">';
            if (!seen[r.book]) {
                html += '<button type="button" class="opt-sp-btn opt-sp-btn-act" data-act="correct" data-book="' +
                    esc(r.book) + '">更正</button>';
                seen[r.book] = 1;
            }
            html += '</td></tr>';
        }
        html += '</tbody></table>';
        return wrapSec('当前持仓', '', wrapTable(html));
    }

    function exitLinesOf(r) {
        if (r.exitLines && r.exitLines.length) return r.exitLines;
        return String(r.exit || '').split(/\s*[\/→；;]\s*/).filter(Boolean);
    }

    function ruleGrid(d) {
        var rows = d.rules || [];
        if (!rows.length) return '';
        var scale = capitalScaleOf(d);
        var html = '<div class="opt-sp-rule-grid">';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var acc = ruleAccentOf(r.name);
            var exitText = (r.exit && String(r.exit).trim()) || exitLinesOf(r).join(' / ');
            var wLab = String(r.weightPct || r.weight || '').trim();
            if (wLab && wLab.indexOf('仓位') < 0) wLab = '仓位 ' + wLab;
            if (wLab && wLab.indexOf('独立账') < 0) wLab += ' 独立账';
            html += '<article class="opt-sp-rule-card' + (acc ? ' opt-sp-rule-card--' + acc : '') + '">' +
                '<div class="opt-sp-rule-top">' +
                '<h4 class="opt-sp-rule-name">' + esc(r.name) + '</h4>' +
                (wLab ? '<span class="opt-sp-chip">' + esc(wLab) + '</span>' : '') +
                (r.tenorHint ? '<span class="opt-sp-chip">' + esc(r.tenorHint) + '</span>' : '') +
                btChipHtml(r.bt1y, scale, 'book') +
                '</div>' +
                '<dl class="opt-sp-rule-dl">' +
                '<dt>开仓</dt><dd>' + esc(r.open) + '</dd>' +
                '<dt>禁开</dt><dd>' + esc(r.ban) + '</dd>' +
                '<dt>平仓</dt><dd>' + esc(exitText) + '</dd>' +
                '</dl></article>';
        }
        html += '</div>';
        return wrapSec(
            '<span class="opt-sp-h-lead">开平仓规则与仓位<span class="opt-sp-chip">一律次月钟</span>' +
            btInlineHtml(d.comboBt1y, scale) + '</span>',
            '',
            html
        );
    }

    function signalRowsHtml(rows) {
        if (!rows || !rows.length) {
            return '<p class="opt-sp-empty">暂无历史信号</p>';
        }
        var html = '<div class="opt-sp-sig-list">';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var tags = r.signals || [];
            var onlyDanger = tags.length === 1 && tags[0] === '危险日';
            var sig = tags.join(' + ') || '—';
            html += '<article class="opt-sp-sig-card' + (r.danger ? ' is-danger' : '') + '">' +
                '<div class="opt-sp-sig-card-top">' +
                '<span class="opt-sp-sig-date">' + esc(r.asof) + '</span>' +
                '<span class="opt-sp-sig-spot">' + esc(fmtNum(r.spot, 3)) + '</span></div>' +
                '<div class="opt-sp-sig-card-bot">' +
                (onlyDanger ? '' : '<span class="opt-sp-chip opt-sp-chip--sig">' + esc(sig) + '</span>') +
                (r.danger ? '<span class="opt-sp-sig-danger">危险日</span>' : '') +
                '</div></article>';
        }
        html += '</div>';
        return html;
    }

    function sigTable(d) {
        var rows = d.recentSignals || [];
        return wrapSec(
            '最近操作信号',
            '<button type="button" class="opt-sp-btn opt-sp-btn-hist" id="opt-sp-hist-btn">历史信号</button>',
            signalRowsHtml(rows)
        );
    }

    function tradeTable(d) {
        var rows = d.recentTrades || [];
        if (!rows.length) {
            return wrapSec(
                '确认成交流水',
                '',
                '<p class="opt-sp-empty">尚无确认成交记录。开盘做完后点「确认成交」手填权利金。</p>'
            );
        }
        var html = '<table class="opt-sp-table opt-sp-table--dense"><thead><tr>' +
            '<th>动作</th><th>执行日</th><th>信号</th><th>到期月</th><th>K</th><th>张</th><th>成交价</th><th>盈亏</th><th>持仓天</th><th class="opt-sp-th-fill">原因</th>' +
            '</tr></thead><tbody>';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var pnl = r.pnl == null ? '—' : (Number(r.pnl) >= 0 ? '+' : '') + Number(r.pnl).toFixed(0);
            if (r.pnlMark && r.pnl != null) pnl += ' 盯';
            html += '<tr><td>' + esc(r.action) + '</td><td>' + esc(r.execDate) + '</td><td>' +
                esc(r.signalName) + '</td><td>' + esc(fmtTenor(r)) + '</td><td>' + esc(fmtNum(r.K, 3)) +
                '</td><td>' + esc(r.lots) + '</td><td>' + esc(fmtNum(r.fillPrem, 4)) +
                '</td><td' + (r.pnlMark ? ' title="未平仓盯市"' : '') + '>' + esc(pnl) +
                '</td><td>' + esc(r.holdDays == null ? '—' : r.holdDays) +
                '</td><td class="opt-sp-td-text">' + esc(r.reason || '') + '</td></tr>';
        }
        html += '</tbody></table>';
        return wrapSec('确认成交流水', '', wrapTable(html));
    }

    function renderDisabled(d) {
        stopPreview();
        host.innerHTML =
            '<header class="opt-sp-head">' +
            '<h2 class="opt-sp-title">Sell Put 操作计划</h2>' +
            '<span class="opt-sp-head-item">' + esc(d.u) + ' 未接入</span></header>' +
            '<p class="opt-sp-empty">' + esc(d.message || '本标的未接入') + '</p>';
    }

    function renderEnabled(d) {
        var wan = (d.capitalWan != null) ? clampCapitalWan(d.capitalWan) : loadCapitalWan();
        saveCapitalWan(wan);
        var html = '<header class="opt-sp-head">' +
            '<h2 class="opt-sp-title">Sell Put 操作计划</h2>' +
            '<label class="opt-sp-head-item opt-sp-capital-lab">每' +
            '<input id="opt-sp-capital-wan" class="opt-sp-capital-wan" type="number" min="10" max="10000" step="10" value="' +
            wan + '">万</label>' +
            '<span class="opt-sp-head-item">已收盘 ' + esc(d.asof) + '</span>' +
            '<span class="opt-sp-head-item">' +
            esc(d.nextTradeDate ? ('下一交易日：' + d.nextTradeDate) : (d.planFor || '')) +
            '</span>' +
            '</header>' +
            '<div id="opt-sp-preview-host"></div>' +
            '<div class="opt-sp-pulse">' +
            '<div class="opt-sp-flags">' + flagChips(d) + '</div>' +
            occBar(d.occupancy, d.occupancyCapPct) +
            '</div>' +
            statusPills(d) +
            openTable(d) +
            closeTable(d) +
            holdTable(d) +
            ruleGrid(d) +
            sigTable(d) +
            tradeTable(d);
        host.innerHTML = html;
        bind(d);
        schedulePreview();
    }

    function previewOpenList(rows) {
        if (!rows || !rows.length) return '';
        var html = '<div class="opt-sp-open-list">';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            var legs = r.legs || [];
            var contracts = legs.length
                ? legs.map(fmtContractLine).join(' / ')
                : (r.tenorLabel || fmtTenor(r));
            var ks = legs.length
                ? uniqJoin(legs.map(function (x) { return fmtNum(x.K, 3); }), ' / ')
                : '—';
            var lots = legs.length
                ? uniqJoin(legs.map(function (x) { return String(x.lots); }), ' / ')
                : String(r.lots == null ? '' : r.lots);
            html += '<article class="opt-sp-open-card">' +
                '<div class="opt-sp-open-id">' +
                '<span class="opt-sp-chip opt-sp-chip--sig">' + esc(fmtBookSignal(r)) + '</span>' +
                '<span class="opt-sp-open-act">预演卖认沽</span></div>' +
                '<dl class="opt-sp-open-fields">' +
                openFld('合约', contracts) +
                openFld('行权价 K', ks) +
                openFld('张', lots) +
                openFld('名义本金', fmtWan(r.notional)) +
                openFld('出口', r.exitHint || r.exitProfile || '—', r.exitProfile, 'opt-sp-fld--exit') +
                '</dl></article>';
        }
        html += '</div>';
        return wrapSec('预演开仓', '', html);
    }

    function previewCloseTable(rows) {
        if (!rows || !rows.length) return '';
        var html = '<table class="opt-sp-table"><thead><tr>' +
            '<th>信号</th><th>动作</th><th>合约</th><th>到期月</th><th>张</th>' +
            '<th class="opt-sp-th-fill">原因</th><th>入场</th><th>现价</th></tr></thead><tbody>';
        for (var i = 0; i < rows.length; i++) {
            var r = rows[i];
            html += '<tr>' +
                '<td>' + esc(fmtBookSignal(r)) + '</td>' +
                '<td>预演买回</td>' +
                '<td>' + esc(r.tsCode) + '</td>' +
                '<td>' + esc(fmtTenor(r)) + '</td>' +
                '<td>' + esc(r.lots) + '</td>' +
                '<td class="opt-sp-td-text">' + esc(r.reason) + '</td>' +
                '<td>' + esc(fmtNum(r.entryPrem, 4)) + '</td>' +
                '<td>' + esc(fmtNum(r.refPrem, 4)) + '</td></tr>';
        }
        html += '</tbody></table>';
        return wrapSec('预演平仓', '', wrapTable(html));
    }

    function renderPreview(p) {
        var el = document.getElementById('opt-sp-preview-host');
        if (!el) return;
        if (!p || !p.active) {
            el.innerHTML = '';
            return;
        }
        var notes = [];
        if (p.dangerPending) notes.push('危险日未确认，Gap 开仓收盘可能作废');
        if (p.premSource === 'opt_daily') notes.push('权利金昨收');
        if (!(p.opens || []).length && !(p.closes || []).length) {
            notes.push('通道/Gap 未触发');
        }
        var meta = (p.clock || '实时') +
            (p.spot != null ? ' · 现货 ' + fmtNum(p.spot, 3) : '') +
            (p.brkH ? ' · 通道突破' : '') +
            (p.gapExt ? ' · Gap≥P' + (p.gapPThr != null ? p.gapPThr : 95) : '');
        el.innerHTML =
            '<div class="opt-sp-preview">' +
            '<div class="opt-sp-preview-head">' +
            '<span class="opt-sp-preview-title">盘中预演</span>' +
            '<span>' + esc(meta) + '</span>' +
            (notes.length ? '<span class="opt-sp-preview-note">' + esc(notes.join('；')) + '</span>' : '') +
            '</div>' +
            previewOpenList(p.opens) +
            previewCloseTable(p.closes) +
            '</div>';
    }

    function stopPreview() {
        if (previewTimer) {
            clearInterval(previewTimer);
            previewTimer = null;
        }
        previewSeq += 1;
        var el = document.getElementById('opt-sp-preview-host');
        if (el) el.innerHTML = '';
    }

    function loadPreview() {
        if (!lastData || !lastData.enabled || !lastU) return;
        if (previewInflight) return;
        var seq = ++previewSeq;
        var url = apiPrefix() + '/api/opt-sp-preview?u=' + encodeURIComponent(lastU) +
            '&capitalWan=' + encodeURIComponent(String(loadCapitalWan()));
        previewInflight = true;
        fetch(url, { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (j) {
                if (seq !== previewSeq) return;
                renderPreview(j && j.preview);
            })
            .catch(function () {
                if (seq !== previewSeq) return;
            })
            .then(function () { previewInflight = false; });
    }

    function schedulePreview() {
        if (previewTimer) {
            loadPreview();
            return;
        }
        previewTimer = setInterval(loadPreview, 4000);
        loadPreview();
    }

    function bind(d) {
        var hist = document.getElementById('opt-sp-hist-btn');
        if (hist) {
            hist.addEventListener('click', function () {
                openHistModal(d.u);
            });
        }
        var btns = host.querySelectorAll('.opt-sp-btn[data-act]');
        for (var i = 0; i < btns.length; i++) {
            btns[i].addEventListener('click', onConfirmClick);
        }
        var capEl = document.getElementById('opt-sp-capital-wan');
        if (capEl) {
            capEl.addEventListener('change', onCapitalChange);
            capEl.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    capEl.blur();
                }
            });
        }
    }

    function onCapitalChange(e) {
        var el = e.currentTarget;
        var n = clampCapitalWan(el.value);
        el.value = String(n);
        saveCapitalWan(n);
        if (lastData && Number(lastData.capitalWan) === n) return;
        var u = lastU || (lastData && lastData.u);
        if (u) loadPlan(u);
    }

    function histOverlay() {
        return document.getElementById('opt-sp-hist-modal-overlay');
    }

    function histShell() {
        var ov = histOverlay();
        return ov ? ov.querySelector('.opt-sp-hist-modal-shell') : null;
    }

    function loadHistLayout() {
        try {
            var raw = localStorage.getItem(LS_HIST);
            if (!raw) return null;
            var o = JSON.parse(raw);
            if (!o || typeof o !== 'object') return null;
            var w = parseInt(o.w, 10);
            var h = parseInt(o.h, 10);
            var l = parseInt(o.l, 10);
            var t = parseInt(o.t, 10);
            if (!(w >= 320 && h >= 320)) return null;
            return { w: w, h: h, l: isFinite(l) ? l : null, t: isFinite(t) ? t : null };
        } catch (e) {
            return null;
        }
    }

    function saveHistLayout() {
        if (Date.now() - histLayoutOpenTs < 700) return;
        var modal = histShell();
        if (!modal) return;
        var r = modal.getBoundingClientRect();
        if (!(r.width >= 320 && r.height >= 320)) return;
        try {
            localStorage.setItem(
                LS_HIST,
                JSON.stringify({
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    l: Math.round(r.left),
                    t: Math.round(r.top)
                })
            );
        } catch (e) {}
    }

    function applyHistLayout() {
        var modal = histShell();
        if (!modal) return;
        var lay = loadHistLayout();
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

    function bindShellDrag(modal, header, onSave) {
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
            modal.style.position = 'fixed';
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
            if (onSave) onSave();
        });
    }

    function bindHistDragResize() {
        if (histDragBound) return;
        var modal = histShell();
        var header = document.getElementById('opt-sp-hist-modal-header');
        if (!modal || !header) return;
        histDragBound = true;
        bindShellDrag(modal, header, saveHistLayout);
        if (typeof ResizeObserver !== 'undefined') {
            var shellObs = new ResizeObserver(function () {
                clearTimeout(histResizeTimer);
                histResizeTimer = setTimeout(saveHistLayout, 400);
            });
            shellObs.observe(modal);
        }
    }

    function closeHistModal() {
        var overlay = histOverlay();
        if (!overlay) return;
        if (overlay.classList.contains('is-open')) saveHistLayout();
        overlay.classList.remove('is-open');
    }

    function openHistModal(u) {
        var overlay = histOverlay();
        var msg = document.getElementById('opt-sp-hist-modal-msg');
        var box = document.getElementById('opt-sp-hist-modal-table');
        var title = document.getElementById('opt-sp-hist-modal-title');
        if (!overlay || !box) return;
        histLayoutOpenTs = Date.now();
        overlay.classList.add('is-open');
        applyHistLayout();
        bindHistDragResize();
        if (title) title.textContent = '历史信号 · ' + (u || '');
        if (msg) {
            msg.hidden = false;
            msg.textContent = '加载中…';
        }
        box.innerHTML = '';
        fetch(apiPrefix() + '/api/opt-sp-plan?u=' + encodeURIComponent(u || '') + '&hist=all', {
            credentials: 'same-origin'
        })
            .then(function (r) { return r.json(); })
            .then(function (j) {
                if (msg) msg.hidden = true;
                if (!j || !j.ok) {
                    box.innerHTML = '<p class="opt-sp-empty">' + esc((j && j.error) || '加载失败') + '</p>';
                    return;
                }
                if (!j.enabled) {
                    box.innerHTML = '<p class="opt-sp-empty">' + esc(j.message || '未接入') + '</p>';
                    return;
                }
                box.innerHTML = signalRowsHtml(j.recentSignals || []);
            })
            .catch(function () {
                if (msg) msg.hidden = true;
                box.innerHTML = '<p class="opt-sp-empty">网络请求失败</p>';
            });
    }

    function nextTradeDate(asof) {
        var p = String(asof || '').split('-');
        if (p.length !== 3) return '';
        var dt = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
        for (var i = 0; i < 10; i++) {
            dt.setDate(dt.getDate() + 1);
            var w = dt.getDay();
            if (w !== 0 && w !== 6) {
                var m = String(dt.getMonth() + 1);
                var day = String(dt.getDate());
                if (m.length < 2) m = '0' + m;
                if (day.length < 2) day = '0' + day;
                return dt.getFullYear() + '-' + m + '-' + day;
            }
        }
        return '';
    }

    function fillOverlay() {
        return document.getElementById('opt-sp-fill-modal-overlay');
    }

    function fillShell() {
        var ov = fillOverlay();
        return ov ? ov.querySelector('.opt-sp-fill-modal-shell') : null;
    }

    function loadFillLayout() {
        try {
            var raw = localStorage.getItem(LS_FILL);
            if (!raw) return null;
            var o = JSON.parse(raw);
            if (!o || typeof o !== 'object') return null;
            var w = parseInt(o.w, 10);
            var h = parseInt(o.h, 10);
            var l = parseInt(o.l, 10);
            var t = parseInt(o.t, 10);
            if (!(w >= 320 && h >= 200)) return null;
            return { w: w, h: h, l: isFinite(l) ? l : null, t: isFinite(t) ? t : null };
        } catch (e) {
            return null;
        }
    }

    function saveFillLayout() {
        if (Date.now() - fillLayoutOpenTs < 700) return;
        var modal = fillShell();
        if (!modal) return;
        var r = modal.getBoundingClientRect();
        if (!(r.width >= 320 && r.height >= 200)) return;
        try {
            localStorage.setItem(
                LS_FILL,
                JSON.stringify({
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    l: Math.round(r.left),
                    t: Math.round(r.top)
                })
            );
        } catch (e) {}
    }

    function applyFillLayout() {
        var modal = fillShell();
        if (!modal) return;
        var lay = loadFillLayout();
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

    function bindFillDragResize() {
        if (fillDragBound) return;
        var modal = fillShell();
        var header = document.getElementById('opt-sp-fill-modal-header');
        if (!modal || !header) return;
        fillDragBound = true;
        bindShellDrag(modal, header, saveFillLayout);
        if (typeof ResizeObserver !== 'undefined') {
            var shellObs = new ResizeObserver(function () {
                clearTimeout(fillResizeTimer);
                fillResizeTimer = setTimeout(saveFillLayout, 400);
            });
            shellObs.observe(modal);
        }
    }

    function setFillErr(text) {
        var el = document.getElementById('opt-sp-fill-err');
        if (!el) return;
        if (!text) {
            el.hidden = true;
            el.textContent = '';
            return;
        }
        el.hidden = false;
        el.textContent = text;
    }

    function closeFillModal() {
        var ov = fillOverlay();
        if (ov && ov.classList.contains('is-open')) saveFillLayout();
        if (ov) ov.classList.remove('is-open');
        fillPending = null;
        setFillErr('');
        setFillModeUi('');
        var sub = document.getElementById('opt-sp-fill-submit');
        if (sub) sub.disabled = false;
    }

    function renderFillLegs(legs) {
        var box = document.getElementById('opt-sp-fill-table');
        if (!box) return;
        var pxLab = (fillPending && fillPending.mode === 'correct') ? '入场价' : '成交价';
        var html = '<table class="opt-sp-table opt-sp-fill-table"><thead><tr>' +
            '<th>期限</th><th>合约代码</th><th>行权价</th><th>张数</th><th>' + pxLab + '</th>' +
            '</tr></thead><tbody>';
        for (var i = 0; i < legs.length; i++) {
            var lg = legs[i];
            var px = lg.px != null ? lg.px : '';
            html += '<tr data-idx="' + i + '">' +
                '<td>' + esc(fmtTenor(lg)) + '</td>' +
                '<td>' + esc(lg.tsCode) + '</td>' +
                '<td>' + esc(fmtNum(lg.K, 3)) + '</td>' +
                '<td><input class="opt-sp-fill-lots" type="number" min="1" step="1" value="' +
                esc(lg.lots) + '"></td>' +
                '<td><input class="opt-sp-fill-px" type="number" min="0" step="0.0001" value="' +
                esc(px) + '"></td>' +
                '</tr>';
        }
        html += '</tbody></table>';
        box.innerHTML = html;
    }

    function setFillModeUi(mode) {
        var isCorrect = mode === 'correct';
        var dateRow = document.getElementById('opt-sp-fill-date-row');
        if (dateRow) dateRow.hidden = false;
        var dateEl = document.getElementById('opt-sp-fill-date');
        if (dateEl) dateEl.disabled = isCorrect;
        var dateLab = document.querySelector('#opt-sp-fill-date-row label');
        if (dateLab) dateLab.textContent = isCorrect ? '开仓日期' : '交易日期';
        var sub = document.getElementById('opt-sp-fill-submit');
        if (sub) sub.textContent = isCorrect ? '确认更正' : '确认成交';
    }

    function openFillModal(spec) {
        fillPending = spec;
        var ov = fillOverlay();
        if (!ov) return;
        var isCorrect = spec.mode === 'correct';
        setFillModeUi(spec.mode);
        var title = document.getElementById('opt-sp-fill-modal-title');
        if (title) {
            title.textContent = isCorrect
                ? ('更正开仓 · ' + (spec.signalName || spec.book || ''))
                : (spec.action + '确认 · ' + (spec.signalName || spec.book || ''));
        }
        var dateEl = document.getElementById('opt-sp-fill-date');
        if (dateEl) dateEl.value = spec.execDate || '';
        var meta = document.getElementById('opt-sp-fill-meta');
        if (meta) {
            if (isCorrect) {
                meta.textContent = (spec.book ? spec.book + '账 · ' : '') +
                    '只改张数与入场价，不改合约/K/日期；手误会写入流水备注';
            } else {
                meta.textContent = (spec.book ? spec.book + '账 · ' : '') +
                    (spec.action === '开仓' ? '默认填最新收盘价，可改' : '买回价默认填最新收盘，可改');
            }
        }
        renderFillLegs(spec.legs || []);
        setFillErr('');
        fillLayoutOpenTs = Date.now();
        ov.classList.add('is-open');
        applyFillLayout();
        bindFillDragResize();
        var firstPx = ov.querySelector('.opt-sp-fill-px');
        if (firstPx) firstPx.focus();
    }

    function collectFillLegs() {
        var box = document.getElementById('opt-sp-fill-table');
        if (!box || !fillPending) return { error: '弹窗已关闭' };
        var src = fillPending.legs || [];
        var trs = box.querySelectorAll('tbody tr');
        var fills = [];
        for (var i = 0; i < trs.length; i++) {
            var lg = src[i];
            if (!lg) continue;
            var lotsEl = trs[i].querySelector('.opt-sp-fill-lots');
            var pxEl = trs[i].querySelector('.opt-sp-fill-px');
            var lots = Number(lotsEl && lotsEl.value);
            var px = Number(pxEl && pxEl.value);
            if (!isFinite(lots) || lots <= 0) return { error: '张数须为正整数' };
            if (!isFinite(px) || px <= 0) {
                return { error: (fillPending.mode === 'correct' ? '入场价' : '成交价') + '须为正数' };
            }
            var row = {
                tsCode: lg.tsCode,
                tenor: lg.tenor,
                tenorKey: lg.tenorKey || tenorKeyOf(lg.tenor),
                K: lg.K,
                lots: Math.round(lots),
                fillPrem: px
            };
            if (lg.posId != null) row.posId = lg.posId;
            fills.push(row);
        }
        if (!fills.length) return { error: '没有可确认的合约' };
        return { fills: fills };
    }

    function submitFillModal() {
        if (!fillPending) return;
        var isCorrect = fillPending.mode === 'correct';
        var dateEl = document.getElementById('opt-sp-fill-date');
        var execDate = dateEl ? String(dateEl.value || '').trim() : '';
        if (!isCorrect && !/^\d{4}-\d{2}-\d{2}$/.test(execDate)) {
            setFillErr('日期格式须为 YYYY-MM-DD');
            return;
        }
        var got = collectFillLegs();
        if (got.error) {
            setFillErr(got.error);
            return;
        }
        var body = {};
        var src = fillPending.body || {};
        for (var k in src) {
            if (Object.prototype.hasOwnProperty.call(src, k)) body[k] = src[k];
        }
        if (!isCorrect) body.execDate = execDate;
        body.fills = got.fills;
        var sub = document.getElementById('opt-sp-fill-submit');
        if (sub) sub.disabled = true;
        setFillErr('');
        var path = isCorrect ? '/api/opt-sp-plan/correct' : '/api/opt-sp-plan/confirm';
        var failText = isCorrect ? '更正失败' : '确认失败';
        postPlan(path, body, function (ok, err) {
            if (sub) sub.disabled = false;
            if (ok) closeFillModal();
            else setFillErr(err || failText);
        });
    }

    function onConfirmClick(ev) {
        var btn = ev.currentTarget;
        var act = btn.getAttribute('data-act');
        var d = lastData;
        if (!d) return;
        var execDate = d.nextTradeDate || nextTradeDate(d.asof);
        if (act === 'open') {
            var idx = Number(btn.getAttribute('data-idx'));
            var row = (d.opens || [])[idx];
            if (!row) return;
            var openLegs = (row.legs || []).map(function (leg) {
                return {
                    tsCode: leg.tsCode,
                    tenor: leg.tenor,
                    tenorKey: leg.tenorKey,
                    K: leg.K,
                    lots: leg.lots,
                    px: leg.px
                };
            });
            openFillModal({
                action: '开仓',
                book: row.book,
                signalName: row.signalName,
                execDate: execDate,
                legs: openLegs,
                body: {
                    u: d.u,
                    book: row.book,
                    action: '开仓',
                    signalName: row.signalName,
                    exitProfile: row.exitProfile,
                    signalDate: d.asof,
                    spot: d.spot
                }
            });
            return;
        }
        var book = btn.getAttribute('data-book');
        if (act === 'correct') {
            var holdLegs = (d.holds || []).filter(function (x) { return x.book === book; });
            if (!holdLegs.length) {
                holdLegs = (d.closes || []).filter(function (x) { return x.book === book; });
            }
            if (!holdLegs.length) return;
            openFillModal({
                mode: 'correct',
                action: '更正开仓',
                book: book,
                signalName: holdLegs[0].signalName,
                execDate: holdLegs[0].entryDate || '',
                legs: holdLegs.map(function (h) {
                    return {
                        posId: h.id,
                        tsCode: h.tsCode,
                        tenor: h.tenor,
                        tenorKey: h.tenorKey,
                        K: h.K,
                        lots: h.lots,
                        px: h.entryPrem
                    };
                }),
                body: { u: d.u, book: book }
            });
            return;
        }
        var closeLegs = (d.closes || []).filter(function (x) { return x.book === book; });
        if (!closeLegs.length) return;
        openFillModal({
            action: '平仓',
            book: book,
            signalName: closeLegs[0].signalName,
            execDate: execDate,
            legs: closeLegs.map(function (c) {
                return {
                    tsCode: c.tsCode,
                    tenor: c.tenor,
                    tenorKey: c.tenorKey,
                    K: c.K,
                    lots: c.lots,
                    px: c.refPrem
                };
            }),
            body: {
                u: d.u,
                book: book,
                action: '平仓',
                reason: closeLegs[0].reason,
                spot: d.spot
            }
        });
    }

    function postPlan(path, body, onDone) {
        body.capitalWan = loadCapitalWan();
        fetch(apiPrefix() + path, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
            .then(function (r) { return r.json(); })
            .then(function (j) {
                if (!j || !j.ok) {
                    var err = (j && j.error) ? j.error : '确认失败';
                    if (onDone) onDone(false, err);
                    else window.alert(err);
                    return;
                }
                lastData = j;
                renderEnabled(j);
                if (onDone) onDone(true);
            })
            .catch(function () {
                if (onDone) onDone(false, '网络请求失败');
                else window.alert('网络请求失败');
            });
    }

    function render(d) {
        lastData = d;
        if (!d || !d.ok) {
            stopPreview();
            host.innerHTML = '<p class="opt-sp-empty">计划加载失败' +
                (d && d.error ? '：' + esc(d.error) : '') + '</p>';
            return;
        }
        if (!d.enabled) {
            renderDisabled(d);
            return;
        }
        renderEnabled(d);
    }

    function spPageCfg() {
        var el = document.getElementById('opt-t-config');
        try {
            if (el && el.textContent) return JSON.parse(el.textContent) || {};
        } catch (e) {}
        return {};
    }

    function spEnabledSet() {
        var codes = spPageCfg().spCodes;
        if (Array.isArray(codes) && codes.length) return new Set(codes);
        return new Set(['588000', '510500', '159915']);
    }

    function spEnabledLabelText() {
        return spPageCfg().spEnabledLabel || '588000 / 510500 / 159915';
    }

    function planCacheKey(u) {
        return String(u || '') + '|' + String(loadCapitalWan());
    }

    function fetchPlan(u) {
        var key = planCacheKey(u);
        if (planInflight[key]) return planInflight[key];
        var url = apiPrefix() + '/api/opt-sp-plan?u=' + encodeURIComponent(u || '') +
            '&hist=5&capitalWan=' + encodeURIComponent(String(loadCapitalWan()));
        function fromNet() {
            return fetch(url, { credentials: 'same-origin' })
                .then(function (r) { return r.json(); });
        }
        var starter = null;
        if (window.__optSpPlanEarly && window.__optSpPlanEarlyKey === key) {
            starter = window.__optSpPlanEarly;
            window.__optSpPlanEarly = null;
        }
        var p = (starter ? starter : fromNet()).then(function (d) {
            if (d && d.ok) {
                planCache[key] = d;
                return d;
            }
            if (starter) return fromNet();
            return d;
        }).then(function (d) {
            if (d && d.ok) planCache[key] = d;
            return d;
        }).finally(function () { delete planInflight[key]; });
        planInflight[key] = p;
        return p;
    }

    function bootSpPlan() {
        host = document.getElementById('opt-sp-plan-inner');
        var boot = window.__optSpPlanBoot;
        if (boot && boot.ok) {
            planCache[planCacheKey(boot.u)] = boot;
        }
        var u = window.__optSpPlanPendingU || '';
        if (!u) {
            var root = document.getElementById('opt-t-body-root');
            if (root) u = root.getAttribute('data-underlying') || '';
        }
        if (u) loadPlan(u);
    }

    function loadPlan(u, opts) {
        opts = opts || {};
        var silent = !!opts.silent;
        if (!host) host = document.getElementById('opt-sp-plan-inner');
        if (!host) return;
        var code = u || '';
        var seq = planSeq;
        if (!silent) {
            closeHistModal();
            closeFillModal();
            lastU = code;
            seq = ++planSeq;
        }
        if (!spEnabledSet().has(code)) {
            if (!silent) {
                renderDisabled({
                    ok: true,
                    enabled: false,
                    u: code,
                    message: '本标的未接入 Sell Put（已接 ' + spEnabledLabelText() + '）'
                });
            }
            return;
        }
        var cached = planCache[planCacheKey(code)];
        if (!silent) {
            if (cached) render(cached);
            else host.textContent = '加载操作计划…';
        }
        fetchPlan(code)
            .then(function (d) {
                if (silent) {
                    if (lastU === code) render(d);
                    return;
                }
                if (seq !== planSeq) return;
                render(d);
            })
            .catch(function () {
                if (silent || seq !== planSeq) return;
                if (planCache[planCacheKey(code)]) return;
                host.innerHTML = '<p class="opt-sp-empty">计划网络请求失败</p>';
            });
    }

    window.__optSpPlanLoad = loadPlan;
    window.__optSpPlanPrefetch = function (u) {
        if (!spEnabledSet().has(u || '')) return;
        loadPlan(u, { silent: true });
    };

    bootSpPlan();

    document.addEventListener('DOMContentLoaded', function () {
        host = document.getElementById('opt-sp-plan-inner');
        var closeBtn = document.getElementById('opt-sp-hist-modal-close');
        if (closeBtn) closeBtn.addEventListener('click', closeHistModal);
        var fillClose = document.getElementById('opt-sp-fill-modal-close');
        if (fillClose) fillClose.addEventListener('click', closeFillModal);
        var fillCancel = document.getElementById('opt-sp-fill-cancel');
        if (fillCancel) fillCancel.addEventListener('click', closeFillModal);
        var fillSubmit = document.getElementById('opt-sp-fill-submit');
        if (fillSubmit) fillSubmit.addEventListener('click', submitFillModal);
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            var fov = fillOverlay();
            if (fov && fov.classList.contains('is-open')) {
                closeFillModal();
                return;
            }
            var ov = histOverlay();
            if (ov && ov.classList.contains('is-open')) closeHistModal();
        });
    });
})();
// 代码更新时间：2026-08-27 13:22 内存缓存+未接入秒渲染；预取不刷屏
// 代码更新时间：2026-08-27 14:20 预演请求防叠，未完成不发下一轮
// 代码更新时间：2026-08-28 06:57 预演轮询 8s，对齐服务端 TTL
// 代码更新时间：2026-08-28 07:08 预演轮询 4s
// 代码更新时间：2026-09-02 07:49 159915：通道强调/L10/Gap分位/三标的回退
// 代码更新时间：2026-09-23 07:53 自启动当前标的；复用 head 早发请求与壳页缓存
// 代码更新时间：2026-09-23 07:59 early 失败回退网络；壳页命中则不抢跑
