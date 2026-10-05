/**
 * 执行 rt_monitor.js 里成交额表着色函数，断言阈值与日期比较。
 * 用法: node tests/test_amount_table_tone.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const srcPath = path.join(__dirname, '..', 'static', 'js', 'rt_monitor.js');
const src = fs.readFileSync(srcPath, 'utf8');
const start = src.indexOf('/* amount-tone-start */');
const end = src.indexOf('/* amount-tone-end */');
const shrinkAt = src.indexOf('function formatAmountShrinkPct');
const gapStart = src.indexOf('/* gap-per-tone-start */');
const gapEnd = src.indexOf('/* gap-per-tone-end */');
if (start < 0 || end < 0 || shrinkAt < 0 || shrinkAt > start || gapStart < 0 || gapEnd < 0) {
    console.error('FAIL markers');
    process.exit(1);
}
const code = src.slice(shrinkAt, end) + '\n' + src.slice(gapStart, gapEnd);
const ctx = { isFinite: Number.isFinite, Number: Number, String: String, parseFloat: parseFloat };
vm.createContext(ctx);
vm.runInContext(code, ctx);

let failed = 0;
function assertEq(actual, expected, msg) {
    if (actual !== expected) {
        console.error('FAIL', msg, 'actual=' + JSON.stringify(actual), 'expected=' + JSON.stringify(expected));
        failed += 1;
    }
}

assertEq(ctx.amountShrinkTone(60, 100), 'rise', '60 red');
assertEq(ctx.amountShrinkTone(63.6, 100), 'rise', '63.6 red');
assertEq(ctx.amountShrinkTone(59.96, 100), 'rise', 'display rounds to 60.0');
assertEq(ctx.amountShrinkTone(20, 100), 'fall', '20 green');
assertEq(ctx.amountShrinkTone(11.2, 100), 'fall', '11.2 green');
assertEq(ctx.amountShrinkTone(20.04, 100), 'fall', 'display rounds to 20.0');
assertEq(ctx.amountShrinkTone(41.5, 100), '', '41.5 plain');
assertEq(ctx.amountShrinkTone(20.06, 100), '', 'display rounds to 20.1');
assertEq(ctx.amountShrinkTone(59.94, 100), '', 'display rounds to 59.9');
assertEq(ctx.amountShrinkTone(null, 100), '', 'null shrink');
assertEq(ctx.amountShrinkTone(10, 0), '', 'zero max');

assertEq(ctx.amountPeakDateTone('2026-07-10', '2026-07-10'), 'rise', 'same peak day');
assertEq(ctx.amountPeakDateTone('2026-07-10 00:00:00', '2026-07-10'), 'rise', 'datetime slice');
assertEq(ctx.amountPeakDateTone('2026-06-22', '2026-07-10'), '', 'different peak');
assertEq(ctx.amountPeakDateTone('', '2026-07-10'), '', 'empty hist');

assertEq(ctx.amountValleyDateTone('2026-09-30', '2026-09-30'), 'fall', 'valley is T-1');
assertEq(ctx.amountValleyDateTone('2026-09-29', '2026-09-30'), '', 'valley is T-2');
assertEq(ctx.amountValleyDateTone('2026-09-30', ''), '', 'missing T-1 date');

assertEq(ctx.amountTodayTone(14417, 14147), 'rise', 'T above T-1');
assertEq(ctx.amountTodayTone(14147, 14147), 'rise', 'T equals T-1');
assertEq(ctx.amountTodayTone(100, 14147), 'fall', 'T below T-1 green');
assertEq(ctx.amountTodayTone(null, 14147), '', 'T null');
assertEq(ctx.amountTodayTone(100, null), '', 'T-1 null');

assertEq(ctx.formatGapPer(87.8), '88', 'gap per round');
assertEq(ctx.formatGapPer(94), '94', 'gap per int');
assertEq(ctx.formatGapPer(null), '', 'gap per null');
assertEq(ctx.gapPerTone(95), 'rise', 'per 95 red');
assertEq(ctx.gapPerTone(94.6), 'rise', 'per rounds to 95');
assertEq(ctx.gapPerTone(5), 'fall', 'per 5 green');
assertEq(ctx.gapPerTone(0), 'fall', 'per 0 green');
assertEq(ctx.gapPerTone(6), '', 'per 6 plain');
assertEq(ctx.gapPerTone(94), '', 'per 94 plain');
assertEq(ctx.gapPerTone(null), '', 'per null plain');

if (failed) {
    console.error('failed', failed);
    process.exit(1);
}
console.log('ok');
