# coding: utf-8
"""tdx_opt_live 五档健康：薄盘不锁 peak；覆盖率低可强刷。"""
from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime
from pathlib import Path


def _loadMod():
    path = Path(__file__).resolve().parents[1] / "tdx_opt_live.py"
    spec = importlib.util.spec_from_file_location("tdx_opt_live_health", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TdxOptHealthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def setUp(self):
        m = self.mod
        m._peakFive = 0
        m._healthBadStreak = 0
        m._coverageLowStreak = 0
        m._staleLowStreak = 0
        m._lastRefreshMono = 1000.0
        m._refreshRequested = False
        m._refreshRequestReason = ""

    def testThinAuctionDoesNotArmPeak(self):
        m = self.mod
        listedN = 226
        m.noteOptHealth(listedN, fiveN=10, errN=0)
        self.assertEqual(m._peakFive, 0)
        need, reason = m.decideForceRefresh(listedN)
        self.assertFalse(need)
        self.assertEqual(m._coverageLowStreak, 1)

    def testCoverageLowTriggersRefresh(self):
        """盘内覆盖率过低 → 强刷。必须打桩时段，否则盘后跑测试会失去意义。"""
        import time
        from unittest.mock import patch

        m = self.mod
        listedN = 226
        # 冷却已过；跳过时段强刷，专测覆盖率恶化
        m._lastRefreshMono = time.monotonic() - (m.REFRESH_COOLDOWN_SEC + 1)
        m._lastSessionRefreshKey = m.sessionForceRefreshKey() or "test-skip"
        for _ in range(m.COVERAGE_LOW_STREAK):
            m.noteOptHealth(listedN, fiveN=0, errN=0)
        with patch.object(m, "inOptSession", lambda *a, **k: True):
            need, reason = m.decideForceRefresh(listedN)
        self.assertTrue(need)
        self.assertIn("coverage_low", reason)

    def testHealthyFiveArmsPeak(self):
        m = self.mod
        listedN = 100
        m.noteOptHealth(listedN, fiveN=90, errN=0)
        self.assertEqual(m._peakFive, 90)
        self.assertEqual(m._coverageLowStreak, 0)

    def testMapTdxRecMapsVolInStockToOi(self):
        m = self.mod
        packed = m.mapTdxRecToPacked(
            {
                "ErrorId": 0,
                "Now": 0.12,
                "Volume": 100,
                "VolInStock": 5555,
                "Buyp": [0.11],
                "Sellp": [0.13],
                "Buyv": [1],
                "Sellv": [2],
            },
            "510500",
        )
        self.assertIsNotNone(packed)
        self.assertEqual(packed.get("vol"), 100)
        self.assertEqual(packed.get("oi"), 5555)
        self.assertEqual(packed.get("openInt"), 5555)

    def testMapKeepsTimeWhenQuoteUnchanged(self):
        import time as timeMod

        m = _loadMod()
        prev = m.mapTdxRecToPacked(
            {
                "ErrorId": 0,
                "Now": 0.12,
                "Volume": 100,
                "Buyp": [0.11],
                "Sellp": [0.13],
            },
            "510050",
        )
        self.assertIsNotNone(prev)
        timeMod.sleep(0.02)
        again = m.mapTdxRecToPacked(
            {
                "ErrorId": 0,
                "Now": 0.12,
                "Volume": 100,
                "Buyp": [0.11],
                "Sellp": [0.13],
            },
            "510050",
            prev=prev,
        )
        self.assertEqual(again.get("time"), prev.get("time"))
        moved = m.mapTdxRecToPacked(
            {
                "ErrorId": 0,
                "Now": 0.13,
                "Volume": 101,
                "Buyp": [0.12],
                "Sellp": [0.14],
            },
            "510050",
            prev=prev,
        )
        self.assertNotEqual(moved.get("time"), prev.get("time"))

    def testMapAdvancesTimeWhenDeepBookChanges(self):
        """买一卖一不变时，二至五档价量变化仍是盘口变化。"""
        m = _loadMod()
        oldNow = datetime(2026, 9, 22, 10, 0, 0)
        newNow = datetime(2026, 9, 22, 10, 0, 1)
        base = {
            "ErrorId": 0,
            "Now": 0.12,
            "Volume": 100,
            "Buyp": [0.11, 0.10, 0.09, 0.08, 0.07],
            "Sellp": [0.13, 0.14, 0.15, 0.16, 0.17],
            "Buyv": [1, 2, 3, 4, 5],
            "Sellv": [5, 4, 3, 2, 1],
        }
        prev = m.mapTdxRecToPacked(base, "159915", now=oldNow)
        changed = dict(base)
        changed["Buyp"] = [0.11, 0.101, 0.09, 0.08, 0.07]
        changed["Sellv"] = [5, 40, 3, 2, 1]
        packed = m.mapTdxRecToPacked(
            changed, "159915", now=newNow, prev=prev,
        )
        self.assertEqual(packed.get("time"), newNow.timestamp() * 1000.0)
        self.assertNotEqual(packed.get("time"), prev.get("time"))


    def testSessionRefreshKeyRollsWithinSession(self):
        """回归（2026-09-11）：**当周期刷新开启时**，强刷键必须按槽位滚动。

        原实现只在开盘/午盘边界各刷一次，而 TDX 本地行情缓存约 10 分钟才自更新
        → 09:45 后整链（含 510500 的 A 类合约）长期停在旧值。

        注意：2026-09-11 起 `TDX_OPT_REFRESH_SESSION_SEC` 默认 **0（关闭）**，
        所以这里显式打开一个非零槽位再测滚动语义，不受默认值变化影响。
        """
        from datetime import datetime, timedelta

        m = self.mod
        saved = m.REFRESH_SESSION_SEC
        m.REFRESH_SESSION_SEC = 3.0
        try:
            base = datetime(2026, 9, 11, 10, 0, 0)
            k1 = m.sessionForceRefreshKey(base)
            # 同槽位内（间隔 < 半个槽位）键不变
            half = max(1.0, m.REFRESH_SESSION_SEC / 2.0)
            k2 = m.sessionForceRefreshKey(base + timedelta(seconds=half))
            # 跨槽位（超过一个槽位）必须变化
            k3 = m.sessionForceRefreshKey(
                base + timedelta(seconds=m.REFRESH_SESSION_SEC + 1.0)
            )
            self.assertEqual(k1, k2)
            self.assertNotEqual(k1, k3)
            # 盘前/盘后不产生键
            self.assertEqual(
                m.sessionForceRefreshKey(datetime(2026, 9, 11, 9, 0, 0)), "")
            self.assertEqual(
                m.sessionForceRefreshKey(datetime(2026, 9, 11, 16, 0, 0)), "")
            # 下午盘同样滚动
            p1 = m.sessionForceRefreshKey(datetime(2026, 9, 11, 13, 30, 0))
            p2 = m.sessionForceRefreshKey(
                datetime(2026, 9, 11, 13, 30, 0)
                + timedelta(seconds=m.REFRESH_SESSION_SEC + 1.0)
            )
            self.assertNotEqual(p1, p2)
            # 冷却必须不大于槽位，否则周期刷新会被冷却吃掉
            self.assertLessEqual(m.REFRESH_COOLDOWN_SEC, m.REFRESH_SESSION_SEC)
        finally:
            m.REFRESH_SESSION_SEC = saved

    def testPeriodicRefreshOnByDecision(self):
        """周期强刷默认**开启**（3s）—— 2026-09-14 用户决策（方案 A）。

        背景与依据：TDX 本地缓存**完全不会自更新**。
        决定性排除实验（`scripts/probe_tdx_selfupdate_offline.py`）：关掉周期刷 +
        保险丝阈值临时设 3600s 后，**420 秒内时间戳推进 0 次**、滞后 8.8s→409.2s。
        因此 `refresh_cache` 是唯一让缓存前进的手段，"关闭"与"新鲜"不可兼得。
        用户权衡后选择新鲜度优先，并**已知悉代价**：约每 3s 弹一次通达信刷新框。

        注意：此前记录的"自然脉冲 33s→135s"是误判（把保险丝自己造出的刷新
        当成了外部规律，见 LESSON-017）—— 本测试不再依赖那个错误模型。
        """
        m = self.mod
        self.assertEqual(m.REFRESH_SESSION_SEC, 3.0)
        # 开启时盘中会产生滚动槽位 → 周期性触发
        from datetime import datetime

        self.assertNotEqual(
            m.sessionForceRefreshKey(datetime(2026, 9, 14, 10, 0, 0)), "")
        self.assertNotEqual(
            m.sessionForceRefreshKey(datetime(2026, 9, 14, 14, 0, 0)), "")
        # 冷却必须不大于槽位，否则周期刷会被冷却吃掉
        self.assertLessEqual(m.REFRESH_COOLDOWN_SEC, m.REFRESH_SESSION_SEC)

    def testPeriodicRefreshCanStillBeDisabled(self):
        """仍可退回"不弹框"模式：sec=0 时不产生任何滚动槽位。

        保留这条是因为弹框频率是使用体验问题，用户随时可能再次改变主意；
        退回只需运行时一条命令，不必改代码。
        """
        m = self.mod
        saved = m.REFRESH_SESSION_SEC
        try:
            self.assertEqual(m.setSessionRefreshSec(0), 0.0)
            from datetime import datetime

            self.assertEqual(
                m.sessionForceRefreshKey(datetime(2026, 9, 14, 10, 0, 0)), "")
            self.assertEqual(
                m.sessionForceRefreshKey(datetime(2026, 9, 14, 14, 0, 0)), "")
        finally:
            m.REFRESH_SESSION_SEC = saved

    def testSetSessionRefreshSecRuntimeToggle(self):
        """运行时可开关周期强刷，且关闭时会清掉槽位记账（便于重新开启即生效）。"""
        m = _loadMod()
        saved = m.REFRESH_SESSION_SEC
        try:
            self.assertEqual(m.setSessionRefreshSec(5), 5.0)
            self.assertEqual(m.REFRESH_SESSION_SEC, 5.0)
            m._lastSessionRefreshKey = "20260911-am-123"
            self.assertEqual(m.setSessionRefreshSec(0), 0.0)
            self.assertEqual(m._lastSessionRefreshKey, "")
            # 非法输入不改变现值
            self.assertEqual(m.setSessionRefreshSec("abc"), 0.0)
            # 负数归零
            self.assertEqual(m.setSessionRefreshSec(-7), 0.0)
        finally:
            m.REFRESH_SESSION_SEC = saved

    def testAllFailSafesGatedBySession(self):
        """**所有** fail-safe 补刷都必须盘内才触发（回归 2026-09-12/13 事故）。

        事故经过：`health_bad` 这一支漏了 `inOptSession()` 门控，而盘后快照
        `five=0`（五档全空）让健康判据恒判"恶化" →
        09-12 15:00~23:00 触发 **6734 次**、09-13 08:00 又 8 次，
        等于盘后每 2~4 秒弹一次通达信刷新框、持续 8 小时。

        本测试把每条 fail-safe 路径都显式打桩在**盘外**，逐条断言"不得触发"。
        重要性：若只测其中一条，漏掉的那条就会像这次一样长期潜伏。
        """
        import time as timeMod
        from unittest.mock import patch

        m = self.mod
        listedN = 226

        # 每条 fail-safe 各自的"武装"方式
        def armHealthBad():
            m._healthBadStreak = m.HEALTH_BAD_STREAK
            m._coverageLowStreak = 0
            m._staleLowStreak = 0

        def armCoverageLow():
            m._healthBadStreak = 0
            m._coverageLowStreak = m.COVERAGE_LOW_STREAK
            m._staleLowStreak = 0

        def armStale():
            m._healthBadStreak = 0
            m._coverageLowStreak = 0
            m._staleLowStreak = m.STALE_LOW_STREAK

        def reset():
            m._refreshRequested = False
            m._refreshRequestReason = ""
            m._lastSessionRefreshKey = m.sessionForceRefreshKey() or "test-skip"
            # 必须同时越过冷却与**陈旧补刷的最小间隔**，否则 stale_p75 会被间隔
            # 挡住，"盘内应触发"的反向断言会假失败（实测踩过）。
            back = max(m.REFRESH_COOLDOWN_SEC, m.STALE_REFRESH_MIN_GAP_SEC) + 1
            m._lastRefreshMono = timeMod.monotonic() - back

        cases = [("health_bad", armHealthBad), ("coverage_low", armCoverageLow),
                 ("stale_p75", armStale)]
        for name, arm in cases:
            reset()
            arm()
            with patch.object(m, "inOptSession", lambda *a, **k: False):
                need, reason = m.decideForceRefresh(listedN)
            self.assertFalse(need, f"盘外不得因 {name} 触发补刷，reason={reason}")
            # 反向确认：同一状态下盘内**必须**能触发，证明打桩确实"武装"成功
            reset()
            arm()
            with patch.object(m, "inOptSession", lambda *a, **k: True):
                need2, reason2 = m.decideForceRefresh(listedN)
            self.assertTrue(need2, f"盘内应能因 {name} 触发，reason={reason2}")
            self.assertIn(name, reason2)

    def testSafetyFallbackGatedBySession(self):
        """`safety` 保底刷同样必须盘内门控（同一类缺陷，别只修一处）。"""
        import time as timeMod
        from unittest.mock import patch

        m = self.mod
        saved = m.REFRESH_SAFETY_SEC
        m.REFRESH_SAFETY_SEC = 5.0          # 武装保底
        try:
            m._refreshRequested = False
            m._refreshRequestReason = ""
            m._lastSessionRefreshKey = m.sessionForceRefreshKey() or "test-skip"
            m._healthBadStreak = 0
            m._coverageLowStreak = 0
            m._staleLowStreak = 0
            m._lastRefreshMono = timeMod.monotonic() - (m.REFRESH_SAFETY_SEC + 5)
            with patch.object(m, "inOptSession", lambda *a, **k: False):
                need, reason = m.decideForceRefresh(226)
            self.assertFalse(need, f"盘外不得因 safety 触发，reason={reason}")

            m._lastRefreshMono = timeMod.monotonic() - (m.REFRESH_SAFETY_SEC + 5)
            with patch.object(m, "inOptSession", lambda *a, **k: True):
                need2, reason2 = m.decideForceRefresh(226)
            self.assertTrue(need2, f"盘内应能因 safety 触发，reason={reason2}")
            self.assertEqual(reason2, "safety")
        finally:
            m.REFRESH_SAFETY_SEC = saved
        """陈旧保险丝阈值必须显著大于 TDX 自然脉冲周期，否则会变成新的弹框源。

        2026-09-11 实测：TDX 本地缓存约每 33 秒脉冲一次，锯齿峰值可达 33s+。
        若阈值取 30s，正常锯齿就会反复触发强刷（→ 又变成每 33 秒弹一次框）。
        """
        m = self.mod
        naturalPulseSec = 33.0
        self.assertGreater(m.REFRESH_STALE_P75_SEC, naturalPulseSec * 2,
                           "阈值必须远大于自然脉冲周期")
        self.assertGreaterEqual(m.STALE_LOW_STREAK, 2,
                                "至少连续两轮，避免单轮毛刺触发")

    def testInOptSessionWindows(self):
        """盘内判定：只有 09:25–11:30 与 13:00–15:00 为真。

        收盘边界必须是 15:00（不是 15:05）：收盘后缓存不再脉冲、陈旧度单调上涨，
        若仍判为盘内，陈旧保险丝会在盘后反复弹框。
        """
        from datetime import datetime

        m = self.mod
        inside = ["09:25", "10:00", "11:29", "13:00", "14:59"]
        outside = ["00:00", "09:00", "09:24", "11:30", "12:00",
                   "15:00", "15:04", "15:30"]
        for hm in inside:
            hh, mm = (int(x) for x in hm.split(":"))
            self.assertTrue(
                m.inOptSession(datetime(2026, 9, 11, hh, mm)),
                f"{hm} 应判为盘内")
        for hm in outside:
            hh, mm = (int(x) for x in hm.split(":"))
            self.assertFalse(
                m.inOptSession(datetime(2026, 9, 11, hh, mm)),
                f"{hm} 应判为盘外")

    def testStaleP75TriggersRefreshWhenDataGoesOld(self):
        """陈旧度保险丝：盘内报价整体变旧必须能自愈；**盘外不得触发**。

        判据用 P75 滞后而不是最大/均值 —— 尾盘不成交合约滞后会自然涨到数分钟。
        盘外门控很重要：收盘后没有脉冲，P75 会一直涨并撞阈值，
        若无门控就会变成盘后反复弹框。
        """
        import time as timeMod
        from unittest.mock import patch

        m = self.mod
        listedN = 226
        over = m.REFRESH_STALE_P75_SEC + 10.0

        def armStreak():
            # 必须把上次刷新推得比**双保险的最小间隔**更远，否则会被间隔挡住，
            # 测不出"门控/阈值"本身的行为（实测踩过：漏设导致测试假失败）。
            back = max(m.REFRESH_COOLDOWN_SEC, m.STALE_REFRESH_MIN_GAP_SEC) + 1
            m._lastRefreshMono = timeMod.monotonic() - back
            m._lastSessionRefreshKey = m.sessionForceRefreshKey() or "test-skip"
            m._staleLowStreak = 0
            for _ in range(m.STALE_LOW_STREAK):
                m.noteOptHealth(listedN, fiveN=200, errN=0, staleP75=over)

        # ① 盘内：应变旧 → 触发
        with patch.object(m, "inOptSession", lambda *a, **k: True):
            armStreak()
            need, reason = m.decideForceRefresh(listedN)
        self.assertTrue(need, f"盘内陈旧应触发补刷，reason={reason}")
        self.assertIn("stale_p75", reason)

        # ② 盘外：同样数据陈旧也**不得**触发（避免盘后无意义弹框）
        with patch.object(m, "inOptSession", lambda *a, **k: False):
            armStreak()
            need2, reason2 = m.decideForceRefresh(listedN)
        self.assertFalse(need2, f"盘外不应触发，reason={reason2}")

        # ③ 无时间戳（-1）不得累积陈旧计数
        m._staleLowStreak = 0
        for _ in range(m.STALE_LOW_STREAK + 2):
            m.noteOptHealth(listedN, fiveN=200, errN=0, staleP75=-1.0)
        self.assertEqual(m._staleLowStreak, 0)

        # ④ 实测锯齿峰值必须**低于**阈值（否则保险丝每个周期都触发）
        m._staleLowStreak = 0
        for _ in range(m.STALE_LOW_STREAK + 2):
            m.noteOptHealth(listedN, fiveN=200, errN=0,
                            staleP75=m.OBSERVED_SAWTOOTH_PEAK_SEC)
        self.assertEqual(m._staleLowStreak, 0,
                         "实测锯齿峰值不应触发保险丝")

    def testStaleRefreshRespectsMinGap(self):
        """双保险回归（2026-09-14）：陈旧补刷之间必须满足最小间隔。

        事故：锯齿周期变慢到 ≈135s、阈值 120s 时，保险丝**每个周期**都触发，
        退化成"每 2 分钟弹一次框"的周期刷新（当天 stale_p75 触发 59 次）。
        除上调阈值外，再加最小间隔兜底，防止将来周期再次变慢时重演。
        """
        import time as timeMod
        from unittest.mock import patch

        m = self.mod
        listedN = 226
        over = m.REFRESH_STALE_P75_SEC + 10.0
        m._refreshRequested = False
        m._refreshRequestReason = ""
        m._lastSessionRefreshKey = m.sessionForceRefreshKey() or "test-skip"
        m._staleLowStreak = 0
        for _ in range(m.STALE_LOW_STREAK):
            m.noteOptHealth(listedN, fiveN=200, errN=0, staleP75=over)

        with patch.object(m, "inOptSession", lambda *a, **k: True):
            # 刚刷过（间隔不足）→ 不得再触发
            m._lastRefreshMono = timeMod.monotonic() - (
                m.STALE_REFRESH_MIN_GAP_SEC - 5.0)
            need, reason = m.decideForceRefresh(listedN)
            self.assertFalse(need, f"间隔不足时不应补刷，reason={reason}")
            # 间隔满足 → 应触发
            m._lastRefreshMono = timeMod.monotonic() - (
                m.STALE_REFRESH_MIN_GAP_SEC + 1.0)
            need2, reason2 = m.decideForceRefresh(listedN)
        self.assertTrue(need2, f"间隔满足后应补刷，reason={reason2}")

    def testStaleThresholdExceedsObservedSawtoothPeak(self):
        """阈值必须大于实测锯齿峰值 —— 这是这条教训的**数值化**守卫。

        两次同型事故：09-11（峰值33s/阈值30s→退化成周期刷）、
        09-14（峰值135s/阈值120s→每 2 分钟弹一次）。单靠"记得调大"不可靠，
        因此把"阈值 > 实测峰值"写成断言；更新峰值请同步更新
        `OBSERVED_SAWTOOTH_PEAK_SEC` 并重跑脚本取证。
        """
        m = self.mod
        self.assertGreater(
            m.REFRESH_STALE_P75_SEC, m.OBSERVED_SAWTOOTH_PEAK_SEC,
            f"阈值 {m.REFRESH_STALE_P75_SEC:.0f}s 必须大于实测锯齿峰值 "
            f"{m.OBSERVED_SAWTOOTH_PEAK_SEC:.0f}s")
        self.assertGreater(m.STALE_REFRESH_MIN_GAP_SEC, 0,
                           "最小间隔必须有意义（>0）")

    def testCoverageLowGatedBySession(self):
        """覆盖率判据同样必须盘内门控 —— 否则盘后也会反复弹框。"""
        import time as timeMod
        from unittest.mock import patch

        m = self.mod
        listedN = 226

        def armCoverage():
            m._lastRefreshMono = timeMod.monotonic() - (m.REFRESH_COOLDOWN_SEC + 1)
            m._lastSessionRefreshKey = m.sessionForceRefreshKey() or "test-skip"
            m._coverageLowStreak = 0
            for _ in range(m.COVERAGE_LOW_STREAK):
                m.noteOptHealth(listedN, fiveN=0, errN=0)

        with patch.object(m, "inOptSession", lambda *a, **k: True):
            armCoverage()
            need, reason = m.decideForceRefresh(listedN)
        self.assertTrue(need, f"盘内覆盖过低应触发，reason={reason}")
        self.assertIn("coverage_low", reason)

        with patch.object(m, "inOptSession", lambda *a, **k: False):
            armCoverage()
            need2, reason2 = m.decideForceRefresh(listedN)
        self.assertFalse(need2, f"盘外不应触发，reason={reason2}")

    def testHealthEvaluatedWithMergedUniverse(self):
        """回归：健康判据必须用**合并后**口径。

        原缺陷：`refreshAndSnap` 内用主链口径（listedN=226、实际五档 10~30）
        评估 `coverage_low`（门槛 listedN*0.35=79）→ 每轮都成立、每 6s 弹框。
        取证：2026-09-11 14:59 日志连续 `coverage_low streak=4 five_min=79`。
        因此 `refreshAndSnap` 内**不得**再调用 noteOptHealth（否则口径错 + streak 双增）。
        """
        import inspect

        m = self.mod
        src = inspect.getsource(m.refreshAndSnap)
        body = "\n".join(
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#")
        )
        self.assertNotIn("noteOptHealth(", body,
                         "refreshAndSnap 不应用主链口径评估健康")

    def testStaleP75ComputedFromQuoteTimestamps(self):
        """staleP75Sec：按字段级时钟取滞后，P75 语义正确。"""
        from datetime import datetime

        m = self.mod
        now = datetime(2026, 9, 11, 14, 0, 0)
        refMs = now.timestamp() * 1000.0
        bag = {}
        for i, lagSec in enumerate([1, 2, 3, 4, 5, 6, 7, 8]):
            bag[f"C{i}"] = {"time": refMs - lagSec * 1000.0}
        p75 = m.staleP75Sec(bag, now)
        # 8 个样本，索引 int(8*0.75)=6 → 第 7 个（滞后 7s）
        self.assertAlmostEqual(p75, 7.0, places=1)
        self.assertEqual(m.staleP75Sec({}, now), -1.0)
        self.assertEqual(m.staleP75Sec({"A": {"time": 0}}, now), -1.0)

    def testDecideForceRefreshRepeatsInSession(self):
        """盘中周期性强刷：冷却过后仍能再次触发（不被"记账"永久吃掉）。

        与真实时钟解耦：显式打桩 `sessionForceRefreshKey`，
        否则午休/盘后跑测试时该函数返回空串会让断言失去意义。
        """
        import time as timeMod
        from unittest.mock import patch

        m = _loadMod()
        m._lastRefreshMono = 1000.0
        m._refreshRequested = False
        m._refreshRequestReason = ""
        m._lastSessionRefreshKey = ""
        m._lastSessionRefreshTryMono = 0.0
        nowMono = timeMod.monotonic()
        slot = [f"20260911-am-{int(nowMono)}"]
        with patch.object(m, "sessionForceRefreshKey", lambda *a, **k: slot[0]):
            # 第一次：槽位新 + 已过冷却 → 触发
            need, reason = m.decideForceRefresh(500)
            self.assertTrue(need, f"首刷应触发，reason={reason}")
            # 模拟刚刷完（记到当前时刻）
            m._lastRefreshMono = nowMono
            # 冷却期内再问：不应触发
            need2, _ = m.decideForceRefresh(500)
            self.assertFalse(need2)
            # 冷却过后 + 槽位滚动 → 应能再次触发
            m._lastRefreshMono = nowMono - (m.REFRESH_COOLDOWN_SEC + 1.0)
            slot[0] = f"20260911-am-{int(nowMono) + 1}"
            need3, reason3 = m.decideForceRefresh(500)
        self.assertTrue(need3, f"冷却后应再次触发，reason={reason3}")


if __name__ == "__main__":
    unittest.main()

# 代码更新时间：2026-09-11 10:0x TDX 盘中周期性强刷（session 槽位滚动）
