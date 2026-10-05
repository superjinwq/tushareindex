# coding: utf-8
"""Sell Put 面板「现货」多口径歧义回归（计划区/预演区标签分化）。

背景：同一页两处都曾叫「现货」——
  · 计划区 chip  = opt-sp-plan 的 spot（etf_daily 末日 close，T-1 收盘）
  · 盘中预演     = opt-sp-preview 的 spot（QMT tick，当日实时）
纯 JS 渲染，Python 侧查不到 DOM；故用 node 实跑抽取出来的标签函数验证真实输出。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_JS = _REPO / "static" / "js" / "rt_monitor_opt_sp_plan.js"

CLOSE_CN = "\u6807\u7684\u6536\u76d8"          # 标的收盘
LIVE_CN = "\u73b0\u8d27"                        # 现货
NO_SIGNAL_CN = "\u65e0\u56db\u4fe1\u53f7"       # 无四信号


def _fn(name: str) -> str:
    src = _JS.read_text(encoding="utf-8")
    m = re.search(rf"function {name}\(d\)\s*\{{.*?\n    \}}", src, re.S)
    assert m, f"function {name} not found (front-end structure changed)"
    return m.group(0)


def _runNode(payload: dict) -> str:
    """实跑 flagChips，返回它产出的 HTML（与页面 chip 一致）。"""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed; skip JS behaviour check")
    stub = """
    function fmtNum(v, d) {
        if (v === null || v === undefined || v === '' || isNaN(v)) return '\\u2014';
        return Number(v).toFixed(d === undefined ? 2 : d);
    }
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    """
    script = "\n".join([stub, _fn("planSpotLabel"), _fn("flagChips"),
                        f"console.log(flagChips({json.dumps(payload)}));"])
    r = subprocess.run([node, "-e", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"node failed: {r.stderr[-800:]}"
    return r.stdout.strip()


def _spotChip(html: str) -> str:
    """取 kind=spot 那个 chip 的可见文案。"""
    m = re.search(r'class="opt-sp-chip opt-sp-chip--spot">(.*?)</span>', html)
    assert m, f"spot chip not found in: {html[:300]}"
    return m.group(1)


def _runNodeWithOldLabelStub(payload: dict) -> str:
    """把旧的「现货 x」实现作为同名 function 声明前置，复现改造前的真实 chip 文案。

    用于自证：这些断言在旧实现下确实会失败（守卫有效性，LESSON-010）。
    """
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed; skip JS behaviour check")
    stub = """
    function fmtNum(v, d) { return Number(v).toFixed(d === undefined ? 2 : d); }
    function esc(s) { return String(s == null ? '' : s); }
    function planSpotLabel(d) { return '\\u73b0\\u8d27 ' + fmtNum(d.spot, 3); }
    """
    script = "\n".join([stub, _fn("flagChips"),
                        f"console.log(flagChips({json.dumps(payload)}));"])
    r = subprocess.run([node, "-e", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"node failed: {r.stderr[-800:]}"
    return r.stdout.strip()


class TestSpotChipLabel(unittest.TestCase):
    def testPlanChipNamesCloseAndBarDate(self):
        """计划区必须自称「标的收盘」并带行情日，不能自称现货。"""
        label = _spotChip(_runNode({"spot": 7.742, "asof": "2026-09-10", "flags": {}}))
        self.assertEqual(label, CLOSE_CN + " 7.742 (09-10)")
        self.assertNotIn(LIVE_CN, label)

    def testPlanChipSurvivesMissingAsof(self):
        """缺 asof 时退化为纯收盘标签，不得崩、不得改口径自称现货。"""
        label = _spotChip(_runNode({"spot": 7.742, "flags": {}}))
        self.assertEqual(label, CLOSE_CN + " 7.742")
        self.assertNotIn(LIVE_CN, label)

    def testPlanChipDiffersFromLiveSpotText(self):
        """同一 spot 数值下，计划区标签必须与预演区「现货 x」字符串不同。"""
        label = _spotChip(_runNode({"spot": 7.742, "asof": "2026-09-10", "flags": {}}))
        self.assertNotEqual(label, LIVE_CN + " 7.742")

    def testNoSignalChipStillRendered(self):
        """标签改造不得影响同函数内的其他 chip（无四信号等）。"""
        html = _runNode({"spot": 7.742, "asof": "2026-09-10", "flags": {}})
        self.assertIn(NO_SIGNAL_CN, html)

    def testGuardIsEffectiveAgainstOldLabel(self):
        """守卫有效性：旧实现下 chip 会变成「现货 7.742」，本测试集必然失败。"""
        old = _spotChip(_runNodeWithOldLabelStub({"spot": 7.742, "asof": "2026-09-10", "flags": {}}))
        self.assertEqual(old, LIVE_CN + " 7.742")           # 旧实现确实产出歧义文案
        self.assertNotEqual(old, CLOSE_CN + " 7.742 (09-10)")
        with self.assertRaises(AssertionError):
            self.assertEqual(old, CLOSE_CN + " 7.742 (09-10)")

    def testSourceHasNoLiveSpotChipInFlagChips(self):
        """源码级守卫：flagChips 内不得再出现「现货」字面量。"""
        self.assertNotIn(LIVE_CN, _fn("flagChips"))


if __name__ == "__main__":
    unittest.main()
