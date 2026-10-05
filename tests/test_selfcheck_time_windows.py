# 用途: 守卫
# 保留至: 2027-09-14
# -*- coding: utf-8 -*-
"""自检器时段判据的边界守卫（LESSON-021）。

背景：自检里两条"新鲜度"判据曾用**固定阈值 + 固定时段**，在两项正常行情状态下误报：
  * `灰行<25%`：14:57 时 510050 灰行 26% → FAIL。查两侧确认 TDX 自己的 time
    已是 p50=73s（合约陆续停成交），页面确实该变灰 —— 是行情性质，不是服务问题。
  * `中位年龄<30s`：14:59 三条 QMT 链中位 144s、但 min 仍有 9s（部分合约还在成交），
    14:52 时中位仅 65~109s —— 同样是尾盘合约停成交。

修法：引入 `inCloseTailQuiet()`（14:45–15:00），两条判据共用它。
本文件守的就是"窗口边界必须精确"——否则要么漏报尾盘假 FAIL，要么把白天真问题一起盖掉。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
_SCRIPTS = os.path.join(_REPO, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _at(hm: str) -> datetime:
    return datetime.strptime(f"2026-09-14 {hm}", "%Y-%m-%d %H:%M")


class CloseTailQuietWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import selfcheck_delivery as sc

        cls.sc = sc

    def testWindowBoundariesAreExact(self):
        """窗口是 [14:45, 15:00)：左闭右开。"""
        cases = {
            "09:30": False,
            "11:29": False,
            "13:00": False,
            "14:44": False,   # 紧邻窗口外 → 判据必须仍然生效
            "14:45": True,    # 左边界含
            "14:57": True,    # 实测触发假 FAIL 的时刻
            "14:59": True,
            "15:00": False,   # 右边界不含（此后走盘后分支）
            "15:30": False,
            "23:59": False,
        }
        for hm, want in cases.items():
            got = self.sc.inCloseTailQuiet(_at(hm))
            self.assertEqual(got, want, f"{hm} 期望 {want} 实得 {got}")

    def testWindowDoesNotSwallowWholeSession(self):
        """窗口必须只占交易时段的一小段 —— 防止有人图省事把它放大成"整个下午"。"""
        session = [
            f"{h:02d}:{m:02d}"
            for h, a, b in ((9, 30, 60), (10, 0, 60), (11, 0, 30),
                            (13, 0, 60), (14, 0, 60))
            for m in range(a, b)
        ]
        quiet = [hm for hm in session if self.sc.inCloseTailQuiet(_at(hm))]
        ratio = len(quiet) / len(session)
        self.assertLess(ratio, 0.20, f"静默窗口占了交易时段 {ratio:.0%}，过大：{quiet}")

    def testTradingSessionStillEndsAt1500(self):
        """`inTradingSession` 的收盘边界不能被本次修改带偏。"""
        self.assertTrue(self.sc.inTradingSession(_at("14:59"))) if (
            self.sc.inTradingSession.__code__.co_argcount > 0
        ) else None
        # 该函数无参（读真实时钟），只做无参形态的存在性 + 15:00 语义的源码级断言
        import inspect

        src = inspect.getsource(self.sc.inTradingSession)
        self.assertIn('"15:00"', src, "收盘边界必须仍是 15:00")
        self.assertIn('"09:30"', src, "开盘边界必须仍是 09:30")

    def testBothFreshnessChecksShareTheWindow(self):
        """两条判据必须共用同一个窗口函数 —— 避免以后只改一条、另一条又炸。"""
        import inspect

        src = inspect.getsource(self.sc.checkBoard)
        self.assertIn("inCloseTailQuiet()", src, "checkBoard 未接尾盘窗口")
        srcRuntime = inspect.getsource(self.sc.checkRuntime)
        self.assertIn("inCloseTailQuiet()", srcRuntime, "checkRuntime 未接尾盘窗口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
