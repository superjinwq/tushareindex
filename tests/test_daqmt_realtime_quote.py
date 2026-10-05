# coding: utf-8
import importlib.util
import inspect
import os
import struct
import threading
import time
import unittest
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "大QMT实时行情.py")


def _loadMod():
    spec = importlib.util.spec_from_file_location("daqmt_rt_quote", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WatchdogWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testMorningOpenIncluded(self):
        self.assertTrue(self.mod.isWatchdogWindow(datetime(2026, 8, 21, 9, 15, 0)))

    def testLunchExcluded(self):
        self.assertFalse(self.mod.isWatchdogWindow(datetime(2026, 8, 21, 11, 30, 0)))
        self.assertFalse(self.mod.isWatchdogWindow(datetime(2026, 8, 21, 12, 0, 0)))

    def testAfternoonCloseExcluded(self):
        self.assertTrue(self.mod.isWatchdogWindow(datetime(2026, 8, 21, 14, 59, 59)))
        self.assertFalse(self.mod.isWatchdogWindow(datetime(2026, 8, 21, 15, 0, 0)))

    def testAuctionIncluded(self):
        self.assertTrue(self.mod.isWatchdogWindow(datetime(2026, 8, 21, 9, 20, 0)))

    def testBeforeOpenExcluded(self):
        self.assertFalse(self.mod.isWatchdogWindow(datetime(2026, 8, 21, 9, 14, 59)))


class WatchdogActionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testSilenceBelowThresholdIdle(self):
        thr = self.mod.SILENCE_SECONDS
        self.assertEqual(
            self.mod.decideWatchdogAction(True, thr - 0.1, 0),
            "idle",
        )

    def testSilenceAtThresholdCheckPing(self):
        self.assertEqual(
            self.mod.decideWatchdogAction(True, self.mod.SILENCE_SECONDS, 0),
            "check_ping",
        )

    def testSlowPingIsNotFakeAlive(self):
        self.assertFalse(self.mod.shouldTreatAsFakeAlive(9007.8, 15.8))
        self.assertFalse(self.mod.shouldTreatAsFakeAlive(None, 30.0))
        self.assertFalse(self.mod.shouldTreatAsFakeAlive("busy", 30.0))

    def testFastPingWithSilenceIsFakeAlive(self):
        self.assertTrue(
            self.mod.shouldTreatAsFakeAlive(46.5, self.mod.SILENCE_SECONDS)
        )
        self.assertFalse(
            self.mod.shouldTreatAsFakeAlive(46.5, self.mod.SILENCE_SECONDS - 0.1)
        )

    def testOutsideWindowIdleEvenIfSilent(self):
        self.assertEqual(
            self.mod.decideWatchdogAction(False, 60.0, 0),
            "idle",
        )

    def testSlowPingWithCriticalUniqueStillFakeAlive(self):
        """回归（2026-09-11）：桥满载 ping 3~6s 时，unique 长期个位数仍须自救。

        现场：全推 unique=6 持续数分钟、resub_ok=0——旧逻辑因 ping>500ms
        永久"让路"，全推死了也不重订。unique 只有个位数说明 combo 没订上，
        与 ping 快慢无关。
        """
        slowMs = 6156.8
        silent = self.mod.SILENCE_SECONDS + 175.0
        # unique 个位数 → 必须判为假活，允许真退真订
        self.assertTrue(
            self.mod.shouldTreatAsFakeAlive(slowMs, silent, uniqueN=6)
        )
        self.assertTrue(
            self.mod.shouldTreatAsFakeAlive(slowMs, silent, uniqueN=0)
        )
        # unique 已过临界值 → 尊重旧的"让路"语义，避免再堵 ROUTER
        self.assertFalse(
            self.mod.shouldTreatAsFakeAlive(
                slowMs, silent, uniqueN=self.mod.QUOTE_CRITICAL_UNIQUE_N
            )
        )
        self.assertFalse(
            self.mod.shouldTreatAsFakeAlive(slowMs, silent, uniqueN=7000)
        )
        # 未提供 unique（-1）时保持旧行为
        self.assertFalse(self.mod.shouldTreatAsFakeAlive(slowMs, silent))
        # 静默不足时任何情况都不重订
        self.assertFalse(
            self.mod.shouldTreatAsFakeAlive(
                slowMs, self.mod.SILENCE_SECONDS - 1.0, uniqueN=0
            )
        )

    def testFastPingStillFakeAliveRegardlessOfUnique(self):
        self.assertTrue(
            self.mod.shouldTreatAsFakeAlive(46.5, self.mod.SILENCE_SECONDS, uniqueN=7000)
        )

    def testQuoteUniqueCriticallyLowBypassesPing(self):
        """回归（2026-09-11）：unique 个位数必须能绕过 ping 直接重订。

        现场：ping 因 _rpcLock 被期权 RPC 占用一直返回 busy → 旧逻辑永远
        走不到重订分支，全推 unique=1 持续十余分钟不自愈。
        """
        self.assertTrue(self.mod.quoteUniqueCriticallyLow(0))
        self.assertTrue(self.mod.quoteUniqueCriticallyLow(1))
        self.assertTrue(self.mod.quoteUniqueCriticallyLow(2))
        self.assertFalse(
            self.mod.quoteUniqueCriticallyLow(self.mod.QUOTE_CRITICAL_UNIQUE_N)
        )
        self.assertFalse(self.mod.quoteUniqueCriticallyLow(7000))
        # 缺失/无意义输入不得触发（-1 表示「未知」）
        self.assertFalse(self.mod.quoteUniqueCriticallyLow(-1))
        self.assertFalse(self.mod.quoteUniqueCriticallyLow(None))
        self.assertFalse(self.mod.quoteUniqueCriticallyLow("busy"))

    def testBusySilenceTriggersResub(self):
        """回归（2026-09-11）：ping 持续 busy + 全推静默 必须能强制重订。

        现场：`_rpcLock` 被期权 RPC 长期占用 → tryRpcPingMs 永远 busy →
        旧逻辑只打 PING_BUSY 日志，全推死了十余分钟不自愈。
        """
        silent = self.mod.SILENCE_SECONDS + 200.0
        self.assertFalse(self.mod.shouldResubOnBusySilence(0, silent))
        self.assertFalse(self.mod.shouldResubOnBusySilence(1, silent))
        self.assertFalse(self.mod.shouldResubOnBusySilence(2, silent))
        self.assertTrue(self.mod.shouldResubOnBusySilence(3, silent))
        self.assertTrue(self.mod.shouldResubOnBusySilence(9, silent))
        # 静默不足不许重订
        self.assertFalse(
            self.mod.shouldResubOnBusySilence(9, self.mod.SILENCE_SECONDS - 1.0)
        )
        self.assertFalse(self.mod.shouldResubOnBusySilence(None, silent))
        self.assertFalse(self.mod.shouldResubOnBusySilence("busy", silent))

    def testWatchdogBypassComesBeforePing(self):
        """顺序保证：unique 极低的重订分支必须在 tryRpcPingMs 之前。"""
        import inspect

        src = inspect.getsource(self.mod._runWatchdogLoop)
        self.assertIn("quoteUniqueCriticallyLow", src)
        # 最近一次 tryRpcPingMs 调用（第三处）必须晚于 unique 极低的重订分支
        self.assertLess(
            src.rindex("quoteUniqueCriticallyLow"),
            src.rindex("tryRpcPingMs"),
        )

    def testGiveUpAfterThreeFails(self):
        self.assertEqual(
            self.mod.decideWatchdogAction(True, 30.0, 3),
            "give_up",
        )

    def testGiveUpHoldsBeforeCooldown(self):
        self.assertEqual(self.mod.decideGiveUpNext(100.0, 50.0, 60.0), "hold")
        self.assertEqual(self.mod.decideGiveUpNext(100.0, 0.0, 60.0), "hold")

    def testGiveUpRetriesAfterCooldown(self):
        self.assertEqual(self.mod.decideGiveUpNext(160.0, 100.0, 60.0), "retry")

    def testPingFailBlocksSubscribe(self):
        self.assertFalse(self.mod.canSubscribeAfterUnsubscribe(False, True))

    def testUnsubFailBlocksSubscribe(self):
        self.assertFalse(self.mod.canSubscribeAfterUnsubscribe(True, False))

    def testPingAndUnsubOkAllowsSubscribe(self):
        self.assertTrue(self.mod.canSubscribeAfterUnsubscribe(True, True))


class EffectiveSilenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testAfternoonIgnoresLunchSilence(self):
        now = datetime(2026, 8, 24, 13, 0, 5)
        rawSec = 90 * 60
        nowMono = 10000.0
        lastPushMono = nowMono - rawSec
        silence = self.mod.effectiveSilenceSec(now, nowMono, lastPushMono)
        self.assertAlmostEqual(silence, 5.0, places=3)

    def testAfternoonUsesLastPushIfAfterWindowStart(self):
        now = datetime(2026, 8, 24, 13, 0, 20)
        nowMono = 10000.0
        lastPushMono = nowMono - 4.0
        silence = self.mod.effectiveSilenceSec(now, nowMono, lastPushMono)
        self.assertAlmostEqual(silence, 4.0, places=3)

    def testLunchWindowStartIsNone(self):
        now = datetime(2026, 8, 24, 12, 0, 0)
        self.assertIsNone(self.mod.watchdogWindowStart(now))

    def testNeverPushedOutsideWindowIsZero(self):
        now = datetime(2026, 8, 24, 12, 0, 0)
        self.assertAlmostEqual(
            self.mod.effectiveSilenceSec(now, 10000.0, 0.0, 9000.0),
            0.0,
            places=3,
        )

    def testNeverPushedInGraceIsZero(self):
        now = datetime(2026, 8, 24, 10, 0, 0)
        nowMono = 10000.0
        silence = self.mod.effectiveSilenceSec(
            now, nowMono, 0.0, nowMono - 5.0, 20.0,
        )
        self.assertAlmostEqual(silence, 0.0, places=3)

    def testNeverPushedAfterGraceUsesWindow(self):
        now = datetime(2026, 8, 24, 10, 0, 0)
        nowMono = 10000.0
        silence = self.mod.effectiveSilenceSec(
            now, nowMono, 0.0, nowMono - 21.0, 20.0,
        )
        self.assertAlmostEqual(silence, 45 * 60.0, places=3)

    def testMorningWindowStart(self):
        now = datetime(2026, 8, 24, 10, 0, 0)
        start = self.mod.watchdogWindowStart(now)
        self.assertEqual(start, datetime(2026, 8, 24, 9, 15, 0))


class WholeQuoteKeepaliveHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testSubscribeLooksNoop(self):
        self.assertTrue(self.mod.subscribeLooksNoop(0.052))
        self.assertFalse(self.mod.subscribeLooksNoop(0.80))

    def testDeferOptionWhenQuoteDead(self):
        self.assertTrue(self.mod.shouldDeferOptionHeavyRpc(0, True))
        self.assertFalse(self.mod.shouldDeferOptionHeavyRpc(12, True))
        self.assertFalse(self.mod.shouldDeferOptionHeavyRpc(0, False))
        self.assertTrue(self.mod.shouldDeferOptionHeavyRpc(12, True, silenceSec=8.0))
        self.assertTrue(self.mod.shouldDeferOptionHeavyRpc(12, True, pingBusy=True))
        self.assertTrue(self.mod.shouldDeferOptionHeavyRpc(12, True, quoteHold=True))
        self.assertFalse(self.mod.shouldDeferOptionHeavyRpc(12, True, silenceSec=7.9))
        self.assertFalse(self.mod.shouldDeferOptionHeavyRpc(12, False, silenceSec=30.0))
        self.assertFalse(self.mod.shouldDeferOptionHeavyRpc(12, False, pingBusy=True))
        self.assertTrue(self.mod.shouldDeferOptionHeavyRpc(12, False, quoteHold=True))
        self.assertEqual(self.mod.OPT_YIELD_SILENCE_SEC, 8.0)
        self.assertEqual(self.mod.OPT_LIVE_TICK_MAX_CHUNKS, 2)
        self.assertEqual(self.mod.OPT_QUOTE_SUB_UNDLS, ("588000", "510500", "510300"))

    def testQtyPatchYieldsWhenWholeQuoteIsStale(self):
        """OI 补丁与全推共用 QMT 策略线程，假活时不可继续抢占它。"""
        self.assertTrue(self.mod.shouldDeferOptionHeavyRpc(7000, True, silenceSec=8.0))
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(True, True, yieldQuote=True)
        )
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(True, True, quoteHold=True)
        )
        self.assertTrue(
            self.mod.shouldRunOptionQtyPatch(True, True, yieldQuote=False)
        )
        # 全市场 whole-quote 仍健康时，期权 subscribe_quote callback 也可能单独归零。
        # 此时不能让 OI get_market_data_ex 继续占用同一 QMT 策略线程。
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(
                True, True, yieldQuote=False, optionCallbackEmpty=True
            )
        )
        # 受控恢复已经失败并进入稳定 TDX fallback 后，不应永久停掉 OI。
        # whole-quote / hold / active recovery 仍是硬安全门。
        self.assertTrue(
            self.mod.shouldRunOptionQtyPatch(
                True,
                True,
                yieldQuote=False,
                optionCallbackEmpty=True,
                optionCallbackFallback=True,
            )
        )

    def testOptChunkOiGateDefersWhen159915OiPressureIsHigh(self):
        """OI 等待/P0年龄已高时，非关键 OPT_CHUNK 应先让出共享 RPC。"""
        m = self.mod
        decision = m.optionChunkOiGateDecision(
            {
                "observedMono": 100.0,
                "demandN": 18,
                "demandWaitP95Sec": 22.0,
                "p0OiAgeMaxSec": 36.0,
                "gapSec": 4.0,
            },
            nowMono=105.0,
            lastActionMono=100.0,
            inWindow=True,
            enabled=True,
        )
        self.assertTrue(decision["defer"])
        self.assertFalse(decision["probe"])
        self.assertIn("demand_wait", decision["reason"])
        self.assertEqual(decision["nextActionMono"], 100.0)

    def testOptChunkOiGateAllowsBoundedProbeAfterInterval(self):
        """压力持续时到达探测间隔，只放行一次受控 OPT_CHUNK 探测。"""
        m = self.mod
        decision = m.optionChunkOiGateDecision(
            {
                "observedMono": 100.0,
                "demandN": 18,
                "demandWaitP95Sec": 22.0,
                "p0OiAgeMaxSec": 36.0,
                "gapSec": 4.0,
            },
            nowMono=116.0,
            lastActionMono=100.0,
            inWindow=True,
            enabled=True,
        )
        self.assertFalse(decision["defer"])
        self.assertTrue(decision["probe"])
        self.assertEqual(decision["nextActionMono"], 116.0)

    def testOptChunkOiGateDoesNotUseStalePressureOrDisabledGate(self):
        """诊断快照过期或显式关闭时不得误拦截行情补齐。"""
        m = self.mod
        stale = m.optionChunkOiGateDecision(
            {
                "observedMono": 0.0,
                "demandN": 18,
                "demandWaitP95Sec": 99.0,
                "p0OiAgeMaxSec": 99.0,
            },
            nowMono=100.0,
            lastActionMono=90.0,
            inWindow=True,
            enabled=True,
        )
        disabled = m.optionChunkOiGateDecision(
            {
                "observedMono": 99.0,
                "demandN": 18,
                "demandWaitP95Sec": 99.0,
                "p0OiAgeMaxSec": 99.0,
            },
            nowMono=100.0,
            lastActionMono=90.0,
            inWindow=True,
            enabled=False,
        )
        self.assertFalse(stale["defer"])
        self.assertEqual(stale["reason"], "pressure_snapshot_stale")
        self.assertFalse(disabled["defer"])
        self.assertEqual(disabled["reason"], "disabled")
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(
                True,
                True,
                yieldQuote=True,
                optionCallbackEmpty=True,
                optionCallbackFallback=True,
            )
        )
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(
                True,
                True,
                quoteRecovery=True,
                optionCallbackEmpty=True,
                optionCallbackFallback=True,
            )
        )
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(False, True, yieldQuote=False)
        )

    def testProbeEtfCodesRejectMarketTokens(self):
        self.assertEqual(self.mod.probeEtfCodesSafe(["SH", "SZ", "BJ"]), [])
        self.assertEqual(
            self.mod.probeEtfCodesSafe(["510050.SH", "SH", "159915.SZ"]),
            ["510050.SH", "159915.SZ"],
        )

    def testShouldProbeWhenUniqueZero(self):
        self.assertTrue(self.mod.shouldProbeEtfTicks(0, 10.0, 0.0, 3.0))
        self.assertFalse(self.mod.shouldProbeEtfTicks(8, 10.0, 0.0, 3.0))
        self.assertFalse(self.mod.shouldProbeEtfTicks(0, 10.0, 8.0, 3.0))
        self.assertTrue(self.mod.shouldProbeEtfTicks(0, 12.0, 8.0, 3.0))
        self.assertTrue(self.mod.shouldProbeEtfTicks(8, 10.0, 0.0, 3.0, silenceSec=8.0))
        self.assertFalse(self.mod.shouldProbeEtfTicks(8, 10.0, 0.0, 3.0, silenceSec=7.9))

    def testProbeInterval(self):
        self.assertEqual(self.mod.etfProbeIntervalSec(True), self.mod.ETF_PROBE_LIVE_SEC)
        self.assertEqual(self.mod.etfProbeIntervalSec(False), self.mod.ETF_PROBE_IDLE_SEC)

    def testApplyEtfProbeDoesNotCountUnique(self):
        self.mod.etfTicks.clear()
        with self.mod._cacheLock:
            self.mod.stats["uniqueCodes"].clear()
            self.mod.stats["lastPushMono"] = 0.0
        n = self.mod.applyEtfProbeTicks({"510050.SH": {"lastPrice": 2.81}})
        self.assertEqual(n, 1)
        with self.mod._cacheLock:
            self.assertEqual(len(self.mod.stats["uniqueCodes"]), 0)
            self.assertEqual(self.mod.stats["lastPushMono"], 0.0)
        self.assertEqual(self.mod.etfTicks["510050.SH"]["lastPrice"], 2.81)


class ResubConfirmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testWaitUntilDeadline(self):
        self.assertEqual(self.mod.decideResubConfirm(10.0, 0.0, 1.0, 15.0), "idle")
        self.assertEqual(self.mod.decideResubConfirm(20.0, 10.0, 9.0, 15.0), "wait")

    def testOkWhenPushAfterResub(self):
        self.assertEqual(self.mod.decideResubConfirm(20.0, 10.0, 10.1, 15.0), "ok")

    def testFailWhenNoPushByDeadline(self):
        self.assertEqual(self.mod.decideResubConfirm(25.0, 10.0, 9.0, 15.0), "fail")


class InstanceLockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testSecondAcquireFailsThenRelease(self):
        path = os.path.join(os.environ.get("TEMP") or ".", "daqmt_rt_quote_test.lock")
        first = self.mod.tryAcquireInstanceLock(path)
        self.assertIsNotNone(first)
        try:
            second = self.mod.tryAcquireInstanceLock(path)
            self.assertIsNone(second)
        finally:
            self.mod.releaseInstanceLock(first)
        third = self.mod.tryAcquireInstanceLock(path)
        self.assertIsNotNone(third)
        self.mod.releaseInstanceLock(third)


class QuoteStealGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testOnlyOwnScript(self):
        own = r"python D:\cursor\tushareindex\大QMT实时行情.py"
        self.assertTrue(self.mod.isDaqmtQuoteCmdline(own))
        self.assertTrue(self.mod.shouldKillQuoteListener(own, 15580))
        self.assertFalse(
            self.mod.isDaqmtQuoteCmdline(r'"D:\bin.x64\XtItClient.exe" reboot')
        )
        self.assertFalse(self.mod.isDaqmtQuoteCmdline("python rt_monitor_app.py"))
        self.assertTrue(self.mod.isQuoteLiveCmdline("python quote-live.py"))
        self.assertFalse(self.mod.isDaqmtQuoteCmdline("python quote-live.py"))
        self.assertFalse(
            self.mod.shouldKillQuoteListener("python rt_monitor_app.py", 15580)
        )
        self.assertTrue(self.mod.shouldKillQuoteListener("python.exe foo.py", 15580))
        self.assertFalse(self.mod.shouldKillQuoteListener("python.exe foo.py", 5000))
        self.assertIn(os.getpid(), self.mod.protectedPids())
        self.assertTrue(self.mod.isPythonProcess(os.getpid()))
        self.assertFalse(self.mod.killPidHard(os.getpid()))
        self.assertFalse(self.mod.killPidHard(os.getppid()))

    def testStealOffByEnv(self):
        self.assertTrue(self.mod.envFlagOn("DAQMT_QUOTE_STEAL_LOCK_TEST_ABSENT", True))


class StartupRpcWaitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def setUp(self):
        self.mod.running = True
        self._sleep = self.mod._sleepInterruptible
        self.mod._sleepInterruptible = lambda _s: None
        self._port = self.mod.isTcpPortOpen
        self.mod.isTcpPortOpen = lambda *_a, **_k: True

    def tearDown(self):
        self.mod.running = True
        self.mod._sleepInterruptible = self._sleep
        self.mod.isTcpPortOpen = self._port

    def testClosedPortNotOpen(self):
        self.mod.isTcpPortOpen = self._port
        self.assertFalse(self.mod.isTcpPortOpen("127.0.0.1", 1, timeoutSec=0.05))

    def testPingOkReturnsTrue(self):
        class Client:
            def call(self, method, params, timeout_seconds=None):
                return {}

        class Trader:
            client = Client()

        import logging

        self.assertTrue(self.mod.waitUntilRpcPing(logging.getLogger("t"), Trader()))

    def testStoppedReturnsFalse(self):
        self.mod.running = False

        class Trader:
            client = None

        import logging

        self.assertFalse(self.mod.waitUntilRpcPing(logging.getLogger("t"), Trader()))

    def testPingRetriesThenOk(self):
        class Client:
            n = 0

            def call(self, method, params, timeout_seconds=None):
                self.n += 1
                if self.n == 1:
                    raise RuntimeError("zmq rpc timeout: ping")
                return {}

        class Trader:
            client = Client()

        import logging

        self.assertTrue(self.mod.waitUntilRpcPing(logging.getLogger("t"), Trader()))
        self.assertEqual(Trader.client.n, 2)

    def testPingRevisionMatchAllowsOptionSub(self):
        class Client:
            def call(self, method, params, timeout_seconds=None):
                return {"rpc_revision": self.mod.EXPECTED_RPC_REVISION, "owned": 0, "cached": 0}

        Client.mod = self.mod

        class Trader:
            client = Client()

        import logging

        self.mod._bridgeRevisionOk = False
        self.assertTrue(self.mod.waitUntilRpcPing(logging.getLogger("t"), Trader()))
        self.assertTrue(self.mod._bridgeRevisionOk)
        self.assertFalse(self.mod.shouldRefuseOptionSubForRevision(self.mod._bridgeRevisionOk))

    def testSubscribeRetriesThenOk(self):
        class Xd:
            n = 0

            def subscribe_whole_quote(self, codes, callback=None, prime=False, force=False):
                self.n += 1
                if self.n == 1:
                    raise RuntimeError("zmq rpc timeout: subscribe_whole_quote")
                time.sleep(0.26)
                return 99

        import logging

        xd = Xd()
        self.assertEqual(
            self.mod.subscribeWholeQuoteUntilOk(logging.getLogger("t"), xd, ["1"]),
            99,
        )
        self.assertEqual(xd.n, 2)

    def testFastSubscribeAckIsKeptForWatchdogConfirmation(self):
        class Xd:
            unsubscribeN = 0

            def subscribe_whole_quote(self, codes, callback=None, prime=False, force=False):
                return 77

            def unsubscribe_quote(self, subId):
                self.unsubscribeN += 1

        import logging

        xd = Xd()
        self.assertEqual(
            self.mod.subscribeWholeQuoteUntilOk(logging.getLogger("t"), xd, ["1"]),
            77,
        )
        self.assertEqual(xd.unsubscribeN, 0)

    def testSubscribeStopsWhenNotRunning(self):
        class Xd:
            def subscribe_whole_quote(self, codes, callback=None, prime=False, force=False):
                raise RuntimeError("timeout")

        def stopSleep(_s):
            self.mod.running = False

        self.mod._sleepInterruptible = stopSleep
        import logging

        self.assertIsNone(
            self.mod.subscribeWholeQuoteUntilOk(logging.getLogger("t"), Xd(), ["1"])
        )


class OptionCodeMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testSseAndSzseSuffix(self):
        self.assertEqual(self.mod.tsCodeToQmtOpt("10011485.SH"), "10011485.SHO")
        self.assertEqual(self.mod.tsCodeToQmtOpt("90007481.SZ"), "90007481.SZO")
        self.assertEqual(self.mod.tsCodeToQmtOpt("10011485.sho"), "10011485.SHO")

    def testRejectEmptyAndEquity(self):
        with self.assertRaises(ValueError):
            self.mod.tsCodeToQmtOpt("")
        with self.assertRaises(ValueError):
            self.mod.tsCodeToQmtOpt("588000.SH")

    def testRowsToQmtDedup(self):
        codes = self.mod.qmtCodesFromOptBasicRows(
            ["10011485.SH", "10011485.SH", "90007481.SZ", "", "588000.SH"]
        )
        self.assertEqual(codes, ["10011485.SHO", "90007481.SZO"])

    def testChunkCodes(self):
        chunks = self.mod.chunkCodes(list(range(5)), 2)
        self.assertEqual(chunks, [[0, 1], [2, 3], [4]])
        self.assertEqual(self.mod.chunkCodes([], 50), [])
        self.assertEqual(len(self.mod.chunkCodes(list(range(728)), 150)), 5)

    def testQmtOptToTsCode(self):
        self.assertEqual(self.mod.qmtOptToTsCode("10011485.SHO"), "10011485.SH")
        self.assertEqual(self.mod.qmtOptToTsCode("90007481.SZO"), "90007481.SZ")

    def testDecodeTickDateNumericString(self):
        from datetime import date as dateCls

        self.assertEqual(
            self.mod.decodeQuoteTickDate("1787278210000"),
            dateCls(2026, 8, 21),
        )

    def testCompactTickUsesMidWhenLastZero(self):
        packed = self.mod.compactQuoteTick(
            {"lastPrice": 0, "bidPrice": [1.1, 1.2], "askPrice": [1.3], "volume": 9, "openInt": 8}
        )
        self.assertAlmostEqual(packed["last"], 1.2)
        self.assertAlmostEqual(packed["lastPrice"], 1.2)
        self.assertEqual(packed["bid1"], 1.1)
        self.assertEqual(packed["ask1"], 1.3)
        self.assertEqual(packed["vol"], 9.0)
        self.assertEqual(packed["oi"], 8.0)

    def testCompactTickKeepsSlimDecisionFields(self):
        packed = self.mod.compactQuoteTick(
            {
                "lastPrice": 1.23,
                "open": 1.20,
                "high": 1.25,
                "low": 1.19,
                "lastClose": 1.22,
                "volume": 100,
                "amount": 12345,
                "openInt": 13,
                "openInterest": 13,
                "stockStatus": 0,
                "bidPrice": [1.22, 1.21, 1.20, 1.19, 1.18, 1.17],
                "bidVol": [1, 2, 3, 4, 5, 6],
                "askPrice": [1.24, 1.25],
                "askVol": [7, 8],
                "time": 1,
                "stime": "09:31:00",
                "pvolume": 999,
                "transactionNum": 88,
            }
        )
        self.assertEqual(packed["lastPrice"], 1.23)
        self.assertEqual(packed["open"], 1.20)
        self.assertEqual(packed["high"], 1.25)
        self.assertEqual(packed["low"], 1.19)
        self.assertEqual(packed["lastClose"], 1.22)
        self.assertEqual(packed["volume"], 100)
        self.assertEqual(packed["amount"], 12345)
        self.assertEqual(packed["openInt"], 13)
        self.assertEqual(packed["stockStatus"], 0)
        self.assertEqual(packed["bidPrice"], [1.22, 1.21, 1.20, 1.19, 1.18])
        self.assertEqual(packed["bidVol"], [1, 2, 3, 4, 5])
        self.assertEqual(packed["stime"], "09:31:00")
        self.assertNotIn("pvolume", packed)
        self.assertNotIn("transactionNum", packed)

    def testCompactTickKeepsFromTdxFlag(self):
        packed = self.mod.compactQuoteTick(
            {
                "lastPrice": 0.08,
                "bidPrice": [0.079],
                "askPrice": [0.081],
                "volume": 3,
                "openInt": 9,
                "time": time.time() * 1000.0,
                "fromTdx": True,
            }
        )
        self.assertTrue(packed.get("fromTdx"))
        packedNo = self.mod.compactQuoteTick(
            {"lastPrice": 0.08, "bidPrice": [0.079], "askPrice": [0.081], "volume": 1}
        )
        self.assertNotIn("fromTdx", packedNo)

    def testOptTickDoesNotFillLastFromMid(self):
        packed = self.mod.compactQuoteTick(
            {"lastPrice": 0, "bidPrice": [0.10], "askPrice": [0.20], "volume": 0},
            fillLastFromMid=False,
        )
        self.assertIsNone(packed["last"])
        self.assertEqual(packed["bid1"], 0.10)
        self.assertEqual(packed["ask1"], 0.20)

    def testOptTickDropsStaleLast(self):
        oldMs = datetime(2026, 8, 21, 10, 10).timestamp() * 1000.0
        packed = self.mod.compactQuoteTick(
            {
                "lastPrice": 0.2041,
                "time": oldMs,
                "volume": 1,
                "openInt": 9,
                "bidPrice": [0.20],
                "askPrice": [0.21],
            },
            fillLastFromMid=False,
            asofDate=datetime(2026, 8, 25).date(),
        )
        self.assertIsNone(packed["last"])
        self.assertAlmostEqual(packed["staleLast"], 0.2041)
        self.assertIsNone(packed["vol"])
        self.assertIsNone(packed["oi"])
        self.assertIsNone(packed["bid1"])
        self.assertIsNone(packed["ask1"])
        self.assertNotIn("bidPrice", packed)

    def testTinyTimeDoesNotDropLast(self):
        packed = self.mod.compactQuoteTick(
            {"lastPrice": 0.20, "time": 1, "volume": 1},
            fillLastFromMid=False,
            asofDate=datetime(2026, 8, 25).date(),
        )
        self.assertAlmostEqual(packed["last"], 0.20)


class OptDailySessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testParseYmdFromIndex(self):
        from datetime import date as dateCls

        bar = self.mod.optionDailyBarFromRecords(
            [
                {
                    "index": "20260825",
                    "close": 0.0167,
                    "volume": 252927,
                    "openInterest": 46941,
                    "preClose": 0.0201,
                    "time": 1787587200000,
                }
            ]
        )
        self.assertEqual(bar["d"], dateCls(2026, 8, 25))
        self.assertEqual(bar["close"], 0.0167)
        self.assertEqual(bar["volume"], 252927.0)
        self.assertEqual(bar["oi"], 46941.0)
        self.assertEqual(bar["preClose"], 0.0201)

    def testStubTodayBarRejected(self):
        from datetime import date as dateCls

        stub = {
            "index": "20260826",
            "close": 0.2948,
            "open": 0.2948,
            "high": 0.2948,
            "low": 0.2948,
            "volume": 0,
            "openInterest": 1878,
            "preClose": 0.0,
            "suspendFlag": 1,
        }
        yday = {
            "index": "20260825",
            "close": 0.2948,
            "volume": 550,
            "openInterest": 1878,
            "preClose": 0.3136,
            "suspendFlag": 0,
        }
        self.assertTrue(self.mod.optionDailyBarLooksStub(
            self.mod.optionDailyBarFromOneRecord(stub),
        ))
        self.assertIsNone(
            self.mod.optionDailyBarFromRecords([yday, stub], asof=dateCls(2026, 8, 26)),
        )

    def testLiveTodayBarKept(self):
        from datetime import date as dateCls

        yday = {
            "index": "20260825",
            "close": 0.2948,
            "volume": 550,
            "openInterest": 1878,
            "preClose": 0.3136,
            "suspendFlag": 0,
        }
        today = {
            "index": "20260826",
            "close": 0.3035,
            "volume": 293,
            "openInterest": 1928,
            "preClose": 0.2948,
            "suspendFlag": 0,
        }
        bar = self.mod.optionDailyBarFromRecords(
            [yday, today], asof=dateCls(2026, 8, 26),
        )
        self.assertEqual(bar["close"], 0.3035)
        self.assertEqual(bar["volume"], 293.0)
        self.assertEqual(bar["oi"], 1928.0)
        self.assertFalse(self.mod.optionDailyBarLooksStub(bar))

    def testNeedDailyDownloadForStubOnly(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 26)
        codes = ["90007481.SZO", "10011855.SHO"]
        daily = {
            "10011855.SHO": {
                "d": asof,
                "close": 0.0149,
                "volume": 100,
                "preClose": 0.0042,
                "suspendFlag": 0,
            },
        }
        need = self.mod.optionCodesNeedingDailyDownload(codes, daily, asof)
        self.assertEqual(need, ["90007481.SZO"])

    def testApplyTodayBarOverwritesStaleTick(self):
        from datetime import date as dateCls

        packed = {
            "last": None,
            "vol": None,
            "oi": None,
            "staleLast": 0.0764,
        }
        daily = {
            "d": dateCls(2026, 8, 25),
            "close": 0.0167,
            "volume": 252927,
            "oi": 46941,
            "preClose": 0.0201,
            "t": 1787587200000,
        }
        out = self.mod.applyOptDailySession(packed, daily, dateCls(2026, 8, 25))
        self.assertEqual(out["last"], 0.0167)
        self.assertEqual(out["vol"], 252927.0)
        self.assertEqual(out["oi"], 46941.0)
        self.assertTrue(out["fromDaily"])
        self.assertNotIn("staleLast", out)

    def testZeroVolumeUsesPrevClose(self):
        from datetime import date as dateCls

        packed = {"last": None, "vol": None}
        daily = {
            "d": dateCls(2026, 8, 25),
            "close": 0.0201,
            "volume": 0,
            "oi": 100,
            "preClose": 0.0201,
        }
        out = self.mod.applyOptDailySession(packed, daily, dateCls(2026, 8, 25))
        self.assertEqual(out["last"], 0.0201)
        self.assertEqual(out["vol"], 0.0)
        self.assertEqual(out["oi"], 100.0)

    def testKeepLiveTickWhenVolAheadOfDaily(self):
        from datetime import date as dateCls

        packed = {
            "last": 0.0170,
            "vol": 260000.0,
            "oi": 40000.0,
            "t": datetime(2026, 8, 25, 14, 0).timestamp() * 1000.0,
        }
        daily = {
            "d": dateCls(2026, 8, 25),
            "close": 0.0167,
            "volume": 252927,
            "oi": 46941,
            "preClose": 0.0201,
        }
        out = self.mod.applyOptDailySession(packed, daily, dateCls(2026, 8, 25))
        self.assertEqual(out["last"], 0.0170)
        self.assertEqual(out["vol"], 260000.0)
        self.assertEqual(out["oi"], 40000.0)
        self.assertFalse(out.get("fromDaily"))

    def testMidnightTickHigherVolBeatsDaily(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 28)
        packed = {
            "last": 0.1359,
            "vol": 120000.0,
            "oi": 62549.0,
            "t": datetime(2026, 8, 28, 0, 0, 0).timestamp() * 1000.0,
        }
        daily = {
            "d": asof,
            "close": 0.1359,
            "volume": 27469,
            "oi": 62549,
            "preClose": 0.1197,
            "t": 1787846400000,
        }
        now = datetime(2026, 8, 28, 10, 30, 0)
        self.assertFalse(self.mod.optionTickIsFresh(packed, asof, now=now))
        out = self.mod.applyOptDailySession(packed, daily, asof, now=now)
        self.assertEqual(out["last"], 0.1359)
        self.assertEqual(out["vol"], 120000.0)
        self.assertFalse(out.get("fromDaily"))

    def testFrozenTickDoesNotMaskDaily(self):
        from datetime import date as dateCls

        packed = {
            "last": 0.136,
            "vol": 2771.0,
            "t": datetime(2026, 8, 27, 14, 0, 30).timestamp() * 1000.0,
        }
        daily = {
            "d": dateCls(2026, 8, 27),
            "close": 0.141,
            "volume": 2800,
            "oi": 1200,
            "preClose": 0.113,
        }
        out = self.mod.applyOptDailySession(packed, daily, dateCls(2026, 8, 27))
        self.assertEqual(out["last"], 0.141)
        self.assertEqual(out["vol"], 2800.0)
        self.assertTrue(out["fromDaily"])

    def testStaleTickYieldsToRicherDaily(self):
        from datetime import date as dateCls

        packed = {
            "last": 0.0150,
            "vol": 1000.0,
            "t": datetime(2026, 8, 25, 10, 0).timestamp() * 1000.0,
        }
        daily = {
            "d": dateCls(2026, 8, 25),
            "close": 0.0167,
            "volume": 252927,
            "oi": 46941,
            "preClose": 0.0201,
        }
        out = self.mod.applyOptDailySession(packed, daily, dateCls(2026, 8, 25))
        self.assertEqual(out["last"], 0.0167)
        self.assertEqual(out["vol"], 252927.0)
        self.assertEqual(out["oi"], 46941.0)
        self.assertTrue(out["fromDaily"])

    def testFreshTickBeatsDaily(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 28)
        now = datetime(2026, 8, 28, 10, 0, 5)
        packed = {
            "last": 0.0584,
            "vol": 18518.0,
            "oi": 9000.0,
            "t": now.timestamp() * 1000.0,
        }
        daily = {
            "d": asof,
            "close": 0.0512,
            "volume": 12000,
            "oi": 9763,
            "preClose": 0.0500,
        }
        self.assertTrue(self.mod.optionTickIsFresh(packed, asof, now=now))
        out = self.mod.applyOptDailySession(
            packed, daily, asof, now=now,
        )
        self.assertEqual(out["last"], 0.0584)
        self.assertEqual(out["vol"], 18518.0)
        self.assertFalse(out.get("fromDaily"))
        self.assertEqual(out["oi"], 9000.0)

    def testStaleClockTickLosesToDaily(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 28)
        now = datetime(2026, 8, 28, 15, 1, 0)
        packed = {
            "last": 0.0512,
            "vol": 8000.0,
            "t": datetime(2026, 8, 28, 14, 0, 0).timestamp() * 1000.0,
        }
        daily = {
            "d": asof,
            "close": 0.0584,
            "volume": 18518,
            "oi": 9763,
            "preClose": 0.0500,
        }
        self.assertFalse(self.mod.optionTickIsFresh(packed, asof, now=now))
        out = self.mod.applyOptDailySession(packed, daily, asof, now=now)
        self.assertEqual(out["last"], 0.0584)
        self.assertTrue(out["fromDaily"])

    def testOctTenXDailyDoesNotCoverBook(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 31)
        packed = {
            "last": 0.1077,
            "vol": 0.0,
            "bid1": 0.1067,
            "ask1": 0.1080,
            "t": datetime(2026, 8, 31, 10, 48).timestamp() * 1000.0,
        }
        daily = {
            "d": asof,
            "close": 1.087,
            "volume": 12,
            "oi": 80,
            "preClose": 0.0,
            "t": datetime(2026, 8, 31, 0, 0).timestamp() * 1000.0,
        }
        now = datetime(2026, 8, 31, 10, 49)
        out = self.mod.applyOptDailySession(
            packed, daily, asof, now=now, listedToday=True
        )
        self.assertAlmostEqual(out["last"], 0.1077)
        self.assertFalse(out.get("fromDaily"))
        self.assertTrue(self.mod.optionDailyCloseConflictsBook(1.087, packed))
        self.assertFalse(
            self.mod.optionDailyLastAllowed(1.74, {"last": None}, listedToday=True)
        )

    def testListedTodayBlankSkipsDailyLast(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 31)
        packed = {"last": None, "vol": None}
        daily = {
            "d": asof,
            "close": 1.74,
            "volume": 3,
            "oi": 10,
            "preClose": 0.0,
        }
        out = self.mod.applyOptDailySession(packed, daily, asof, listedToday=True)
        self.assertIsNone(out.get("last"))
        self.assertFalse(out.get("fromDaily"))
        self.assertEqual(out.get("oi"), 10.0)

    def testListedTodayTenXDailyScaled(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 31)
        packed = {"last": None, "vol": None}
        daily = {
            "d": asof,
            "close": 1.76,
            "volume": 3,
            "oi": 10,
            "preClose": 0.0,
        }
        meta = {"k": 3.4, "cp": "C", "undl": "159915.SZ"}
        self.assertAlmostEqual(
            self.mod.scaleTenXOptLast(1.76, meta, 3.396), 0.176,
        )
        self.assertTrue(
            self.mod.optionDailyLastAllowed(
                1.76, packed, listedToday=True, meta=meta, spot=3.396,
            )
        )
        out = self.mod.applyOptDailySession(
            packed, daily, asof, listedToday=True, meta=meta, spot=3.396,
        )
        self.assertAlmostEqual(out["last"], 0.176)
        self.assertTrue(out.get("fromDaily"))

    def testSzoMinuteTenXRejected(self):
        meta = {"k": 3.4, "cp": "C", "undl": "159915.SZ"}
        self.assertTrue(self.mod.optionPremiumLooksImplausible(1.76, meta, 3.396))
        self.assertFalse(self.mod.optionPremiumLooksImplausible(0.16, meta, 3.396))
        self.assertTrue(
            self.mod.optionPremiumLooksImplausible(
                13.15, {"k": 4.7, "cp": "P"}, 3.396
            )
        )
        self.assertFalse(
            self.mod.optionPremiumLooksImplausible(
                1.31, {"k": 4.7, "cp": "P"}, 3.396
            )
        )
        asof = date(2026, 8, 31)
        minute = {"lastPrice": 1.76, "volume": 3, "time": datetime(2026, 8, 31, 11, 11).timestamp() * 1000.0}
        self.assertTrue(
            self.mod.shouldApplySzoMinuteTick(None, minute, asof, meta, 3.396)
        )
        self.assertAlmostEqual(minute["lastPrice"], 0.176)
        sane = {"lastPrice": 0.16, "volume": 3, "time": datetime(2026, 8, 31, 11, 11).timestamp() * 1000.0}
        self.assertTrue(
            self.mod.shouldApplySzoMinuteTick(None, sane, asof, meta, 3.396)
        )
        scaled = self.mod.dropImplausibleOptLast(
            {"last": 1.76, "vol": 3, "fromDaily": False}, meta, 3.396
        )
        self.assertAlmostEqual(scaled.get("last"), 0.176)
        midPacked = self.mod.dropImplausibleOptLast(
            {
                "last": 1.76,
                "bid1": 0.1067,
                "ask1": 0.1080,
                "vol": 3,
            },
            meta,
            3.396,
        )
        self.assertAlmostEqual(midPacked.get("last"), 0.10735)

    def testMissingTickStampNotFresh(self):
        from datetime import date as dateCls

        packed = {"last": 0.05, "vol": 100.0, "t": None}
        self.assertFalse(
            self.mod.optionTickIsFresh(packed, dateCls(2026, 8, 28))
        )
        self.assertIsNone(self.mod.optionTickStampSec(None))
        ts = datetime(2026, 8, 28, 10, 0, 0).timestamp()
        self.assertAlmostEqual(
            self.mod.optionTickStampSec(ts * 1000.0), ts, places=3,
        )


class EtfDailySessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testFillWhenTickMissing(self):
        from datetime import date as dateCls

        daily = {
            "d": dateCls(2026, 8, 25),
            "close": 1.691,
            "open": 1.70,
            "high": 1.72,
            "low": 1.65,
            "volume": 1e8,
            "preClose": 1.68,
            "t": 1787587200000,
        }
        out = self.mod.applyEtfDailySession({"last": None}, daily, dateCls(2026, 8, 25))
        self.assertEqual(out["last"], 1.691)
        self.assertEqual(out["open"], 1.70)
        self.assertTrue(out["fromDaily"])

    def testKeepTodayTick(self):
        from datetime import date as dateCls

        packed = {
            "last": 1.705,
            "open": 1.70,
            "t": datetime(2026, 8, 25, 14, 0).timestamp() * 1000.0,
        }
        daily = {"d": dateCls(2026, 8, 25), "close": 1.691, "open": 1.69}
        out = self.mod.applyEtfDailySession(packed, daily, dateCls(2026, 8, 25))
        self.assertEqual(out["last"], 1.705)
        self.assertFalse(out.get("fromDaily"))

    def testReplaceYesterdayTick(self):
        from datetime import date as dateCls

        packed = {
            "last": 1.680,
            "t": datetime(2026, 8, 24, 15, 0).timestamp() * 1000.0,
        }
        daily = {
            "d": dateCls(2026, 8, 25),
            "close": 1.691,
            "open": 1.70,
            "preClose": 1.68,
        }
        out = self.mod.applyEtfDailySession(packed, daily, dateCls(2026, 8, 25))
        self.assertEqual(out["last"], 1.691)
        self.assertTrue(out["fromDaily"])

    def testSkipStubDailyBar(self):
        from datetime import date as dateCls

        daily = {
            "d": dateCls(2026, 8, 25),
            "close": 1.691,
            "open": 1.691,
            "high": 1.691,
            "low": 1.691,
            "volume": 0,
            "preClose": 1.691,
        }
        out = self.mod.applyEtfDailySession({"last": None}, daily, dateCls(2026, 8, 25))
        self.assertIsNone(out.get("last"))
        self.assertFalse(out.get("fromDaily"))

    def testRtkRowToday(self):
        from datetime import date as dateCls

        bar = self.mod.etfDailyBarFromRtkRow(
            {
                "close": 1.693,
                "open": 1.666,
                "high": 1.714,
                "low": 1.651,
                "vol": 3.3e9,
                "pre_close": 1.691,
                "trade_time": datetime(2026, 8, 25, 15, 10, 2),
            },
            dateCls(2026, 8, 25),
        )
        self.assertIsNotNone(bar)
        self.assertEqual(bar["close"], 1.693)
        self.assertFalse(self.mod.etfBarLooksStub(bar))

    def testRtkRowWrongDay(self):
        from datetime import date as dateCls

        bar = self.mod.etfDailyBarFromRtkRow(
            {
                "close": 1.693,
                "vol": 1e8,
                "trade_time": datetime(2026, 8, 24, 15, 10, 2),
            },
            dateCls(2026, 8, 25),
        )
        self.assertIsNone(bar)


class OptionPollWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testMorningAndCloseBatch(self):
        self.assertTrue(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 9, 15, 0)))
        self.assertTrue(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 11, 30, 30)))
        self.assertTrue(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 11, 31, 0)))

    def testLunchStaysOnLiveDaily(self):
        self.assertTrue(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 12, 0, 0)))
        self.assertTrue(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 12, 59, 0)))
        self.assertFalse(self.mod.shouldDownloadOptionTicks(datetime(2026, 8, 24, 12, 0, 0)))

    def testLunchHeavyRpcStopsAfterBoundedCatchup(self):
        self.assertTrue(
            self.mod.isOptionHeavyRpcWindow(datetime(2026, 8, 24, 11, 30, 0))
        )
        self.assertTrue(
            self.mod.isOptionHeavyRpcWindow(datetime(2026, 8, 24, 11, 34, 59))
        )
        self.assertFalse(
            self.mod.isOptionHeavyRpcWindow(datetime(2026, 8, 24, 11, 35, 0))
        )
        self.assertFalse(
            self.mod.isOptionHeavyRpcWindow(datetime(2026, 8, 24, 12, 0, 0))
        )
        self.assertTrue(
            self.mod.isOptionHeavyRpcWindow(datetime(2026, 8, 24, 13, 0, 0))
        )
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(
                self.mod.isOptionHeavyRpcWindow(datetime(2026, 8, 24, 12, 0, 0)),
                True,
            )
        )

    def testAfterCloseDisablesQmtCatchupAndKeepsOnlyPreopenBootstrap(self):
        self.assertTrue(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 15, 30, 59)))
        self.assertFalse(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 15, 31, 0)))
        self.assertFalse(self.mod.isOptionPollWindow(datetime(2026, 8, 24, 9, 14, 59)))
        self.assertFalse(self.mod.isOptionCatchupWindow(datetime(2026, 8, 24, 15, 31, 0)))
        self.assertFalse(self.mod.isOptionCatchupWindow(datetime(2026, 8, 24, 16, 0, 0)))
        self.assertFalse(self.mod.isOptionCatchupWindow(datetime(2026, 8, 24, 15, 30, 59)))
        self.assertFalse(self.mod.isOptionCatchupWindow(datetime(2026, 8, 24, 21, 0, 0)))
        self.assertTrue(self.mod.isOptionPreopenBootstrapWindow(datetime(2026, 8, 24, 9, 14, 59)))
        self.assertFalse(self.mod.isOptionPreopenBootstrapWindow(datetime(2026, 8, 24, 9, 15, 0)))
        self.assertFalse(self.mod.isOptionPreopenBootstrapWindow(datetime(2026, 8, 24, 16, 0, 0)))

    def testSkipTickDownloadInSession(self):
        self.assertFalse(self.mod.shouldDownloadOptionTicks(datetime(2026, 8, 24, 9, 15, 0)))
        self.assertFalse(self.mod.shouldDownloadOptionTicks(datetime(2026, 8, 24, 15, 31, 0)))
        self.assertTrue(self.mod.shouldDownloadOptionTicks(datetime(2026, 8, 24, 9, 12, 59)))
        self.assertFalse(
            self.mod.shouldDownloadOptionTicks(datetime(2026, 8, 24, 9, 13, 0)),
            "120 秒 tick RPC 不得在 09:13 后启动并跨越 09:15 watchdog 窗口",
        )

    def testPreopenRpcDeadlineUsesConfiguredWorstCaseBudget(self):
        m = self.mod
        self.assertTrue(
            m.shouldStartPreopenRpc(datetime(2026, 9, 28, 9, 14, 14), 45.0)
        )
        self.assertFalse(
            m.shouldStartPreopenRpc(datetime(2026, 9, 28, 9, 14, 15), 45.0),
            "45 秒日线下载也必须为 09:15 watchdog 留出安全余量",
        )
        self.assertFalse(
            m.shouldStartPreopenRpc(datetime(2026, 9, 28, 9, 15, 0), 1.0)
        )

    def testPreopenTickBootstrapIsBoundedBeforeRpc(self):
        """不能先用整条标的链占锁，失败后才缩到 15 张。"""
        m = self.mod
        codes = [f"100{i:05d}.SHO" for i in range(88)]
        called = []
        tried = set()

        class FakeLogger:
            def info(self, *_args, **_kwargs):
                return None

        saved = {
            "_tickDlTried": m._tickDlTried,
            "optionCodesNeedingTickDownload": m.optionCodesNeedingTickDownload,
            "_copyOptionTicks": m._copyOptionTicks,
            "_copyOptionDaily": m._copyOptionDaily,
            "groupOptionCodesByUnderlying": m.groupOptionCodesByUnderlying,
            "_copyOptionMeta": m._copyOptionMeta,
            "_downloadOptionTickStore": m._downloadOptionTickStore,
        }
        try:
            m._tickDlTried = lambda _asof: tried
            m.optionCodesNeedingTickDownload = lambda *_args: list(codes)
            m._copyOptionTicks = lambda: {}
            m._copyOptionDaily = lambda: {}
            m.groupOptionCodesByUnderlying = lambda *_args: [list(codes)]
            m._copyOptionMeta = lambda: {}

            def fakeDownload(_logger, _xtdata, batch, _start):
                called.append(list(batch))
                return len(batch)

            m._downloadOptionTickStore = fakeDownload
            got = m._maybeDownloadStaleOptionTicks(
                FakeLogger(), object(), codes, date(2026, 9, 28)
            )
        finally:
            for name, value in saved.items():
                setattr(m, name, value)
        self.assertEqual(got, m.OPT_TICK_DL_CHUNK)
        self.assertEqual(len(called), 1)
        self.assertEqual(len(called[0]), m.OPT_TICK_DL_CHUNK)

    def testQueuedBackgroundDownloadYieldsToQuoteHold(self):
        """等待 RPC 锁期间 watchdog 置 hold 后，后台下载不得再抢锁执行。"""
        m = self.mod
        calls = []

        class FakeClient:
            def call(self, *_args, **_kwargs):
                calls.append(1)
                return True

        class FakeXtdata:
            client = FakeClient()

        m._quoteHold.clear()
        m._rpcLock.acquire()
        worker = threading.Thread(
            target=m._downloadOptionPeriodStore,
            args=(None, FakeXtdata(), ["10000001.SHO"], "20260921", "tick", 1.0),
        )
        try:
            worker.start()
            time.sleep(0.05)
            m._quoteHold.set()
        finally:
            m._rpcLock.release()
        worker.join(timeout=1.0)
        m._quoteHold.clear()
        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [], "watchdog hold 后排队下载仍调用了桥接 RPC")

    def testTickGapsAllowedInLiveWindow(self):
        noon = datetime(2026, 8, 27, 13, 0, 0)
        self.assertTrue(self.mod.shouldDownloadOptionTickGaps(noon, 18))
        self.assertFalse(self.mod.shouldDownloadOptionTickGaps(noon, 0))
        self.assertFalse(self.mod.shouldDownloadOptionTickGaps(noon, 572))
        self.assertFalse(
            self.mod.shouldDownloadOptionTickGaps(datetime(2026, 8, 27, 9, 14, 59), 18)
        )
        self.assertFalse(
            self.mod.shouldDownloadOptionTickGaps(datetime(2026, 8, 27, 15, 31, 0), 18)
        )

    def testLiveTickGapRequiresListedToday(self):
        need = ["90000001.SZO", "10000001.SHO", "90000002.SZO", "10000002.SHO"]
        self.assertEqual(self.mod.optionLiveTickGapCodes(need, []), [])
        self.assertEqual(
            self.mod.optionLiveTickGapCodes(need, ["10000001.SHO", "90000002.SZO"]),
            ["10000001.SHO", "90000002.SZO"],
        )

    def testLiveTickGapShoFirst(self):
        need = ["90000001.SZO", "10000001.SHO", "90000002.SZO"]
        self.assertEqual(
            self.mod.optionLiveTickGapCodes(need, need),
            ["10000001.SHO", "90000001.SZO", "90000002.SZO"],
        )

    def testListedTodayRemembersChainAdds(self):
        from datetime import date as dateCls

        d0 = dateCls(2026, 8, 27)
        d1 = dateCls(2026, 8, 28)
        self.mod.rememberOptionChainAdds(["10012319.SHO", "90007069.SZO"], d0)
        self.assertEqual(
            set(self.mod.optionListedTodayCodes(d0)),
            {"10012319.SHO", "90007069.SZO"},
        )
        self.mod.rememberOptionChainAdds(["10019999.SHO"], d1)
        self.assertEqual(self.mod.optionListedTodayCodes(d1), ["10019999.SHO"])
        self.assertEqual(self.mod.optionListedTodayCodes(d0), [])

    def testTickGapChunkCapsAt15(self):
        need = ["c%d" % i for i in range(40)]
        chunk = self.mod.optionTickGapDownloadChunk(need)
        self.assertEqual(len(chunk), 15)
        self.assertEqual(chunk[0], "c0")
        self.assertEqual(chunk[-1], "c14")
        self.assertEqual(self.mod.optionTickGapDownloadChunk(["a", "b"], 15), ["a", "b"])

    def testMergeOptionPollCodesKeepsOrder(self):
        merged = self.mod.mergeOptionPollCodes(["a", "b"], ["b", "c"])
        self.assertEqual(merged, ["a", "b", "c"])

    def testSzoWarmupSkipsSzoDailyAndStub(self):
        codes = ["10011516.SHO", "90007784.SZO", "90007783.SZO"]
        self.assertTrue(self.mod.szoSubscribeInProgress(codes, False, False))
        self.assertFalse(self.mod.szoSubscribeInProgress(codes, True, False))
        self.assertFalse(self.mod.szoSubscribeInProgress(codes, False, True))
        self.assertFalse(self.mod.szoSubscribeInProgress(["10011516.SHO"], False, False))
        self.assertEqual(
            self.mod.optionDailyPollCodes(codes, False, False),
            ["10011516.SHO"],
        )
        self.assertEqual(
            self.mod.optionDailyPollCodes(
                codes, False, False, owned=["90007784.SZO"]
            ),
            ["10011516.SHO", "90007784.SZO"],
        )
        self.assertEqual(
            self.mod.optionDailyPollCodes(
                codes, False, False, listedToday=["90007783.SZO"]
            ),
            ["10011516.SHO", "90007783.SZO"],
        )
        self.assertEqual(self.mod.optionDailyPollCodes(codes, True, False), codes)
        self.assertEqual(self.mod.optionDailyPollCodes(codes, False, True), codes)
        self.assertEqual(
            self.mod.optionStubDailyDownloadCodes(codes, False, False),
            ["10011516.SHO"],
        )
        self.assertEqual(
            self.mod.optionStubDailyDownloadCodes(codes, True, False),
            codes,
        )
        self.assertEqual(self.mod.optionStubDailyDownloadCodes(codes, False, True), codes)
        self.assertEqual(self.mod.optionDailyGateMode(codes, False, False), "sho_only")
        self.assertEqual(self.mod.optionDailyGateMode(codes, True, False), "all")
        self.assertEqual(self.mod.optionDailyGateMode(codes, False, True), "szo_dl_fallback")
        self.assertEqual(self.mod.optionDailyGateMode(["10011516.SHO"], False, False), "all")

    def testSzoDailyStoreRefreshInterval(self):
        self.assertEqual(
            self.mod.optionSzoCodes(["10011516.SHO", "90007784.SZO", "90007783.szo"]),
            ["90007784.SZO", "90007783.szo"],
        )
        self.assertEqual(
            self.mod.optionDailySubscribeCodes(
                ["10011516.SHO", "90007784.SZO", "588000.SH", "10011516.SHO"]
            ),
            ["90007784.SZO"],
        )
        self.assertEqual(self.mod.OPT_SZO_SUB_PERIOD, "tick")
        self.assertEqual(self.mod.OPT_QUOTE_SUB_MAX_OWNED, 500)
        self.assertEqual(self.mod.OPT_SZO_MIN_PERIOD, "1m")
        self.assertEqual(self.mod.OPT_SZO_MIN_BAR_COUNT, 240)
        self.assertEqual(self.mod.OPT_SZO_MIN_DL_CHUNK, 40)
        self.assertEqual(self.mod.OPT_DAILY_WITH_TICK_SEC, 90.0)
        self.assertTrue(self.mod.shouldRefreshSzoDailyStore(100.0, 0.0, 90.0))
        self.assertFalse(self.mod.shouldRefreshSzoDailyStore(80.0, 0.0, 90.0))
        self.assertFalse(self.mod.shouldUseSzoDailyDownload(True, False))
        self.assertFalse(self.mod.shouldUseSzoDailyDownload(False, False))
        self.assertTrue(self.mod.shouldUseSzoDailyDownload(False, True))

    def testSzoSessionTickAndPollUniverse(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 28)
        yest = datetime(2026, 8, 27, 15, 0, 0).timestamp() * 1000.0
        live = datetime(2026, 8, 28, 10, 58, 42).timestamp() * 1000.0
        midnight = datetime(2026, 8, 28, 0, 0, 0).timestamp() * 1000.0
        self.assertFalse(
            self.mod.optionSzoTickHasSessionPrint({"time": yest, "lastPrice": 0.12}, asof)
        )
        self.assertFalse(
            self.mod.optionSzoTickHasSessionPrint({"time": midnight, "volume": 1}, asof)
        )
        self.assertTrue(
            self.mod.optionSzoTickHasSessionPrint({"time": live, "volume": 85932}, asof)
        )
        codes = ["10011516.SHO", "90007075.SZO", "90007076.SZO"]
        self.assertEqual(
            self.mod.optionTickPollCodes(codes),
            ["10011516.SHO"],
        )
        self.assertEqual(
            self.mod.optionTickPollCodes(codes, ["90007075.SZO"]),
            ["10011516.SHO", "90007075.SZO"],
        )
        self.assertEqual(
            self.mod.szoLiveTickDownloadStartYmd(asof),
            "20260828",
        )
        # 深市 subscribe_quote 实测 0 回调，订阅宇宙只留沪市。
        self.assertEqual(
            self.mod.optionQuoteSubscribeCodes(codes),
            [],
        )
        cached = {
            "90007075.SZO": {"lastPrice": 0.11, "volume": 9, "ageSec": 0.4},
            "10011516.SHO": {"lastPrice": 0.06, "volume": 1, "ageSec": 20.0},
        }
        self.assertEqual(
            self.mod.optionCacheMissCodes(codes, cached, 8.0),
            ["10011516.SHO", "90007076.SZO"],
        )

    def testOptionHeavyRpcDeferredAndShoBudget(self):
        self.assertEqual(self.mod.optionSyncAddedN(None), 0)
        self.assertEqual(self.mod.optionSyncAddedN({"added": ["a", "b"]}), 2)
        self.assertTrue(self.mod.optionHeavyRpcDeferred(12))
        self.assertTrue(self.mod.optionHeavyRpcDeferred(12, False))
        self.assertFalse(self.mod.optionHeavyRpcDeferred(0))
        self.assertTrue(self.mod.shouldSkipLiveDailyBars(False, 0, 0))
        self.assertTrue(self.mod.shouldSkipLiveDailyBars(True, 12, 0))
        self.assertTrue(self.mod.shouldSkipLiveDailyBars(True, 0, 2))
        self.assertTrue(self.mod.shouldSkipLiveDailyBars(True, 0, 0, 50))
        self.assertFalse(self.mod.shouldSkipLiveDailyBars(True, 0, 0, 0))
        self.assertEqual(self.mod.OPT_SZO_MIN_STALE_SEC, 8.0)
        self.assertEqual(self.mod.OPT_QUOTE_CACHE_MAX_AGE_SEC, 30.0)
        miss = ["100%04d.SHO" % i for i in range(100)]
        budget = self.mod.optionShoPollBudget(miss, 20, 4)
        self.assertEqual(len(budget), 80)
        self.assertEqual(budget[0], "1000000.SHO")
        rotated = self.mod.optionShoPollBudget(miss, 20, 4, rotate=80)
        self.assertEqual(len(rotated), 80)
        self.assertEqual(rotated[0], "1000080.SHO")
        self.assertNotEqual(rotated[0], budget[0])
        wrap = self.mod.optionShoPollBudget(miss[:10], 20, 4, rotate=8)
        self.assertEqual(wrap[0], miss[8])
        self.assertEqual(wrap[-1], miss[7])
        self.assertEqual(self.mod.optionShoPollBudget(miss, 20, 0), [])
        self.assertEqual(self.mod.OPT_SZO_SUB_ADD_LIMIT, 40)
        self.assertEqual(self.mod.OPT_CHUNK_SIZE, 20)
        self.assertEqual(self.mod.OPT_SHO_POLL_MAX_CHUNKS, 4)
        self.assertEqual(self.mod.OPT_LIVE_TICK_MAX_CHUNKS, 2)
        self.assertEqual(self.mod.OPT_LIVE_REFRESH_CHUNK, 20)
        self.assertEqual(self.mod.OPT_LIVE_DL_CHUNK, 40)
        self.assertEqual(self.mod.OPT_POLL_FAIL_ABORT, 2)
        self.assertGreaterEqual(self.mod.RPC_PROBE_TIMEOUT_SEC, 6.0)
        self.assertEqual(self.mod.OPT_QUOTE_CACHE_EMPTY_RESYNC, 3)
        self.assertEqual(self.mod.OPT_QUOTE_SUB_HEAL_SEC, 90.0)
        self.assertEqual(self.mod.OPT_QUOTE_HEAL_FAIL_STOP, 3)
        self.assertEqual(self.mod.OPT_QUOTE_SUB_FORCE_COOLDOWN_SEC, 90.0)
        self.assertFalse(self.mod.shouldForceQuoteSubResync(90, 9))
        self.assertFalse(self.mod.shouldForceQuoteSubResync(0, 2))
        self.assertFalse(self.mod.shouldForceQuoteSubResync(0, 3))
        self.assertFalse(
            self.mod.shouldForceQuoteSubResync(0, 3, isSubFull=True)
        )
        self.assertFalse(
            self.mod.shouldForceQuoteSubResync(0, 3, isSubFull=True, inAuction=False)
        )
        self.assertTrue(
            self.mod.shouldForceQuoteSubResync(0, 3, isSubFull=True, inAuction=True)
        )
        self.assertFalse(
            self.mod.shouldForceQuoteSubResync(
                0, 3, isSubFull=True, inAuction=True,
                sinceForceSec=10.0, forceCooldownSec=90.0,
            )
        )
        self.assertTrue(
            self.mod.shouldForceQuoteSubResync(
                0, 3, isSubFull=True, inAuction=True,
                sinceForceSec=91.0, forceCooldownSec=90.0,
            )
        )
        self.assertFalse(
            self.mod.shouldForceQuoteSubResync(0, 2, isSubFull=True, inAuction=True)
        )
        self.assertTrue(
            self.mod.pingRevisionMatches(
                {"rpc_revision": self.mod.EXPECTED_RPC_REVISION},
                self.mod.EXPECTED_RPC_REVISION,
            )
        )
        self.assertFalse(
            self.mod.pingRevisionMatches(
                {"rpc_revision": "old"}, self.mod.EXPECTED_RPC_REVISION
            )
        )
        self.assertFalse(self.mod.pingRevisionMatches({}, self.mod.EXPECTED_RPC_REVISION))
        self.assertTrue(self.mod.shouldRefuseOptionSubForRevision(False))
        self.assertFalse(self.mod.shouldRefuseOptionSubForRevision(True))
        self.assertTrue(self.mod.isOptHealthDeadPush(418, 418, 0, True))
        self.assertFalse(self.mod.isOptHealthDeadPush(418, 418, 0, False))
        self.assertFalse(self.mod.isOptHealthDeadPush(418, 418, 12, True))
        self.assertFalse(self.mod.isOptHealthDeadPush(40, 418, 0, True))
        self.assertEqual(self.mod.EXPECTED_RPC_REVISION, "20260903-opt-heal-v2")
        self.assertTrue(
            self.mod.shouldHealUncachedQuoteSub(420, 420, 265, True)
        )
        self.assertFalse(
            self.mod.shouldHealUncachedQuoteSub(420, 420, 0, True)
        )
        self.assertFalse(
            self.mod.shouldHealUncachedQuoteSub(420, 420, 420, True)
        )
        self.assertFalse(
            self.mod.shouldHealUncachedQuoteSub(420, 420, 265, False)
        )
        self.assertFalse(
            self.mod.shouldHealUncachedQuoteSub(200, 420, 100, True)
        )
        self.assertFalse(
            self.mod.shouldHealUncachedQuoteSub(
                420, 420, 265, True, sinceHealSec=10.0, cooldownSec=90.0,
            )
        )
        self.assertFalse(
            self.mod.shouldHealUncachedQuoteSub(
                420, 420, 265, True, healExhausted=True,
            )
        )
        miss = ["10012372.SHO", "10012373.SHO"]
        key1, n1, give1 = self.mod.noteHealUncachedOutcome(
            397, 397, miss, miss, (), 0,
        )
        self.assertEqual(n1, 1)
        self.assertEqual(give1, [])
        key2, n2, give2 = self.mod.noteHealUncachedOutcome(
            397, 397, miss, miss, key1, n1,
        )
        self.assertEqual(n2, 2)
        self.assertEqual(give2, [])
        key3, n3, give3 = self.mod.noteHealUncachedOutcome(
            397, 397, miss, miss, key2, n2,
        )
        self.assertEqual(n3, 3)
        self.assertEqual(set(give3), set(miss))
        self.assertEqual(
            self.mod.noteHealUncachedOutcome(397, 398, miss, miss, key3, n3)[1:],
            (0, []),
        )
        other = ["10011524.SHO"]
        _keyN, nN, giveN = self.mod.noteHealUncachedOutcome(
            397, 397, other, other, key2, 2,
        )
        self.assertEqual(nN, 1)
        self.assertEqual(giveN, [])
        self.assertEqual(
            self.mod.optionQuoteOwnedExtras(["a.SHO"], ["a.SHO", "b.SHO"]),
            ["B.SHO"],
        )
        self.assertEqual(
            self.mod.optionQuoteSubscribeCodes(
                ["10011516.SHO"],
                {"10011516.SHO": {"undl": "588000.SH"}},
                pollOnly=["10011516.SHO"],
            ),
            [],
        )
        self.assertTrue(
            self.mod.shouldSkipHeavyUntilQuoteSubFull(
                ["a.SHO", "b.SHO"], ["a.SHO"], 500
            )
        )
        self.assertFalse(
            self.mod.shouldSkipHeavyUntilQuoteSubFull(
                ["a.SHO", "b.SHO"], ["a.SHO", "b.SHO"], 500
            )
        )
        noon = datetime(2026, 9, 2, 12, 55)
        self.assertFalse(self.mod.isOptionContinuousAuction(noon))
        self.assertTrue(
            self.mod.isOptionContinuousAuction(datetime(2026, 9, 2, 13, 1))
        )
        self.assertTrue(self.mod.isWholeQuoteHealthyForOptDl(7190, 0.4))
        self.assertFalse(self.mod.isWholeQuoteHealthyForOptDl(341, 2.0))
        self.assertFalse(self.mod.isWholeQuoteHealthyForOptDl(7190, 12.0))
        self.assertFalse(
            self.mod.tickHasFiveBand({"bidPrice": [0, 0, 0], "askPrice": [0, 0, 0]})
        )
        self.assertTrue(
            self.mod.tickHasFiveBand({"bidPrice": [0.10, 0], "askPrice": [0.12]})
        )
        liveCodes = ["10011516.SHO", "90007075.SZO", "510050.SH"]
        self.assertEqual(
            self.mod.optionLivePollCodes(liveCodes),
            ["10011516.SHO"],
        )

    def testSzoMinuteNeedStaleChiNextFirst(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 28)
        now = datetime(2026, 8, 28, 13, 12, 11)
        freshMs = datetime(2026, 8, 28, 13, 12, 0).timestamp() * 1000.0
        staleMs = datetime(2026, 8, 28, 13, 8, 0).timestamp() * 1000.0
        ticks = {
            "90007086.SZO": {"time": freshMs, "lastPrice": 0.04, "volume": 1},
            "90007075.SZO": {"time": staleMs, "lastPrice": 0.11, "volume": 9},
        }
        meta = {
            "90007086.SZO": {"undl": "510500.SH"},
            "90007075.SZO": {"undl": "159915.SZ"},
            "90009999.SZO": {"undl": "159915.SZ"},
        }
        codes2 = ["90009999.SZO", "90007086.SZO", "90007075.SZO"]
        ticks2 = dict(ticks)
        ticks2["90009999.SZO"] = {"time": staleMs, "lastPrice": 0.01, "volume": 1}
        need = self.mod.optionSzoMinuteNeedCodes(
            codes2, ticks2, asof, now=now, staleSec=70.0, metaMap=meta,
        )
        self.assertEqual(need[0], "90009999.SZO")
        self.assertEqual(need[1], "90007075.SZO")
        self.assertNotIn("90007086.SZO", need)
        intra = datetime(2026, 8, 28, 13, 12, 40)
        self.assertFalse(
            self.mod.optionSzoMinuteBarStale(
                {"time": freshMs, "volume": 1}, intra, 8.0,
            )
        )
        self.assertTrue(
            self.mod.optionSzoMinuteBarStale(
                {"time": freshMs, "volume": 1},
                datetime(2026, 8, 28, 13, 13, 9),
                8.0,
            )
        )
        cold = self.mod.optionSzoMinuteNeedCodes(
            codes2, ticks2, asof, now=now, staleSec=None, metaMap=meta,
        )
        self.assertEqual(cold, [])
        self.assertEqual(
            self.mod.optionSzoMinuteNeedCodes(
                ["90007506.SZO"], {}, asof, now=now, staleSec=None,
            ),
            ["90007506.SZO"],
        )
        marked = self.mod.optionSzoMinuteMarkFailed(
            ["90007506.SZO"], {}, asof, 100.0, 90.0, {},
        )
        self.assertGreaterEqual(marked["90007506.SZO"], 190.0)
        skipped = self.mod.optionSzoMinuteNeedCodes(
            ["90007506.SZO"],
            {},
            asof,
            now=now,
            staleSec=70.0,
            skipUntil=marked,
            nowMono=100.0,
        )
        self.assertEqual(skipped, [])
        self.assertFalse(self.mod.shouldSkipShoForSzoMinute(7, 20))
        self.assertTrue(self.mod.shouldSkipShoForSzoMinute(20, 20))
        self.assertTrue(
            self.mod.shouldKeepTickOverMinute(
                {
                    "time": datetime(2026, 8, 28, 13, 8, 28).timestamp() * 1000.0,
                    "lastPrice": 0.11,
                    "volume": 1,
                },
                {"time": staleMs, "lastPrice": 0.10, "volume": 2},
                asof,
            )
        )
        laterMin = datetime(2026, 8, 28, 13, 12, 0).timestamp() * 1000.0
        self.assertFalse(
            self.mod.shouldKeepTickOverMinute(
                {
                    "time": datetime(2026, 8, 28, 13, 8, 28).timestamp() * 1000.0,
                    "lastPrice": 0.40,
                    "volume": 17,
                    "bidPrice": [0.39, 0.38],
                    "askPrice": [0.41, 0.42],
                },
                {"time": laterMin, "lastPrice": 4.009, "volume": 17},
                asof,
            )
        )
        needBook = self.mod.optionSzoMinuteNeedCodes(
            ["90007075.SZO"],
            {
                "90007075.SZO": {
                    "time": staleMs,
                    "lastPrice": 0.11,
                    "volume": 9,
                    "bidPrice": [0.11],
                    "askPrice": [0.12],
                },
            },
            asof,
            now=now,
            staleSec=70.0,
        )
        self.assertEqual(needBook, ["90007075.SZO"])

    def testSzoMinuteRecordsToTickSumsTodayVolume(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 28)
        t1 = datetime(2026, 8, 28, 9, 31, 0).timestamp() * 1000.0
        t2 = datetime(2026, 8, 28, 11, 12, 0).timestamp() * 1000.0
        yest = datetime(2026, 8, 27, 14, 0, 0).timestamp() * 1000.0
        preOpen = datetime(2026, 8, 28, 9, 0, 0).timestamp() * 1000.0
        recs = [
            {"time": yest, "close": 0.12, "volume": 99999, "openInterest": 1},
            {"time": preOpen, "close": 0.0, "volume": 0},
            {"time": t1, "close": 0.11, "volume": 10000, "openInterest": float("nan")},
            {"time": t2, "close": 0.1152, "volume": 80274, "openInterest": 42888},
        ]
        tick = self.mod.szoMinuteRecordsToTick(recs, asof)
        self.assertIsNotNone(tick)
        self.assertAlmostEqual(tick["lastPrice"], 0.1152)
        self.assertAlmostEqual(tick["volume"], 90274.0)
        self.assertEqual(tick["openInt"], 42888)
        self.assertEqual(tick["time"], t2)
        self.assertIsNone(self.mod.szoMinuteRecordsToTick([], asof))
        self.assertIsNone(
            self.mod.szoMinuteRecordsToTick(
                [{"time": t2, "close": 0.1, "volume": 0}],
                asof,
            )
        )
        lastNanOi = self.mod.szoMinuteRecordsToTick(
            [
                {"time": t1, "close": 0.11, "volume": 10, "openInterest": 100},
                {"time": t2, "close": 0.12, "volume": 20, "openInterest": float("nan")},
            ],
            asof,
        )
        self.assertEqual(lastNanOi["openInt"], 100)
        self.assertAlmostEqual(lastNanOi["volume"], 30.0)
        self.assertEqual(lastNanOi["time"], t2)
        self.assertEqual(lastNanOi["oiTime"], t1)

    def testLocalMinuteOiAcceptsZeroVolumeWithoutTouchingQuote(self):
        """零成交分钟柱的有效 OI 可推进，但不得改写 TDX 价量盘口。"""
        asof = date(2026, 9, 24)
        code = "90007481.SZO"
        oldMs = datetime(2026, 9, 24, 9, 56).timestamp() * 1000.0
        oiMs = datetime(2026, 9, 24, 9, 59).timestamp() * 1000.0
        existing = {
            "fromTdx": True,
            "last": 0.2510,
            "lastPrice": 0.2510,
            "vol": 123.0,
            "volume": 123.0,
            "bid1": 0.2509,
            "ask1": 0.2512,
            "bidPrice": [0.2509, 0.2508],
            "askPrice": [0.2512, 0.2513],
            "bookTime": oldMs,
            "oi": 7700.0,
            "openInt": 7700.0,
            "oiTime": oldMs,
        }
        records = [{
            "time": oiMs,
            "close": 0.2511,
            "volume": 0,
            "openInterest": 7795,
        }]
        savedRead = self.mod.readQmtLocalBars
        savedTicks = dict(self.mod.optionTicks)
        try:
            self.mod.optionTicks.clear()
            self.mod.optionTicks[code] = dict(existing)
            self.mod.readQmtLocalBars = lambda *_args, **_kwargs: {code: records}

            result = self.mod.applyLocalMinuteOiCandidates(
                [code],
                asof,
                now=datetime(2026, 9, 24, 10, 0, 10),
            )

            self.assertEqual(result["observed"], 1)
            self.assertEqual(result["applied"], 1)
            out = self.mod.optionTicks[code]
            self.assertEqual(out["oi"], 7795)
            self.assertEqual(out["oiTime"], oiMs)
            self.assertEqual(out["oiSource"], "qmt_1m")
            for key in (
                "last", "lastPrice", "vol", "volume", "bid1", "ask1",
                "bidPrice", "askPrice", "bookTime",
            ):
                self.assertEqual(out[key], existing[key])
        finally:
            self.mod.readQmtLocalBars = savedRead
            self.mod.optionTicks.clear()
            self.mod.optionTicks.update(savedTicks)

    def testLocalMinuteOiAlignsFutureBarEndToCurrentMinute(self):
        """QMT 当前未结束分钟柱的结束时刻不得把 OI 标成未来。"""
        asof = date(2026, 9, 29)
        code = "90008141.SZO"
        oldMs = datetime(2026, 9, 29, 9, 52).timestamp() * 1000.0
        barEndMs = datetime(2026, 9, 29, 9, 55).timestamp() * 1000.0
        observedAt = datetime(2026, 9, 29, 9, 54, 14)
        existing = {
            "fromTdx": True,
            "last": 0.08,
            "lastPrice": 0.08,
            "vol": 10,
            "volume": 10,
            "bid1": 0.079,
            "ask1": 0.081,
            "bookTime": oldMs,
            "oi": 100,
            "openInt": 100,
            "oiTime": oldMs,
        }
        records = [{
            "time": barEndMs,
            "close": 0.08,
            "volume": 0,
            "openInterest": 123,
        }]
        savedRead = self.mod.readQmtLocalBars
        savedTicks = dict(self.mod.optionTicks)
        try:
            self.mod.optionTicks.clear()
            self.mod.optionTicks[code] = dict(existing)
            self.mod.readQmtLocalBars = lambda *_args, **_kwargs: {code: records}

            result = self.mod.applyLocalMinuteOiCandidates(
                [code],
                asof,
                now=observedAt,
            )

            self.assertEqual(result["observed"], 1)
            self.assertEqual(result["applied"], 1)
            out = self.mod.optionTicks[code]
            expectedMs = datetime(2026, 9, 29, 9, 54).timestamp() * 1000.0
            self.assertEqual(out["oi"], 123)
            self.assertEqual(out["oiTime"], expectedMs)
            self.assertLessEqual(out["oiTime"], observedAt.timestamp() * 1000.0)
        finally:
            self.mod.readQmtLocalBars = savedRead
            self.mod.optionTicks.clear()
            self.mod.optionTicks.update(savedTicks)

    def testMinuteOiFutureBarOutsideAlignmentWindowFailsClosed(self):
        asof = date(2026, 9, 29)
        observedAt = datetime(2026, 9, 29, 9, 54, 14)
        tooFuture = datetime(2026, 9, 29, 9, 57).timestamp() * 1000.0
        candidate = self.mod.szoMinuteRecordsToOiCandidate(
            [{"time": tooFuture, "openInterest": 123}],
            asof,
            observedAtMs=observedAt.timestamp() * 1000.0,
        )
        self.assertIsNone(candidate)

    def testMinuteOiFutureAlignmentCanBeDisabledForRollback(self):
        asof = date(2026, 9, 29)
        observedAt = datetime(2026, 9, 29, 9, 54, 14)
        barEndMs = datetime(2026, 9, 29, 9, 55).timestamp() * 1000.0
        saved = self.mod.OPT_SZO_MIN_FUTURE_BAR_ALIGN_SEC
        try:
            self.mod.OPT_SZO_MIN_FUTURE_BAR_ALIGN_SEC = 0.0
            candidate = self.mod.szoMinuteRecordsToOiCandidate(
                [{"time": barEndMs, "openInterest": 123}],
                asof,
                observedAtMs=observedAt.timestamp() * 1000.0,
            )
            self.assertEqual(candidate["oiTime"], barEndMs)
        finally:
            self.mod.OPT_SZO_MIN_FUTURE_BAR_ALIGN_SEC = saved

    def testMinuteOiOnlyFlagRestoresZeroVolumeRejection(self):
        """运行开关关闭时恢复旧门槛，便于盘中快速回退。"""
        asof = date(2026, 9, 24)
        oiMs = datetime(2026, 9, 24, 9, 59).timestamp() * 1000.0
        records = [{
            "time": oiMs,
            "close": 0.2511,
            "volume": 0,
            "openInterest": 7795,
        }]
        saved = self.mod.OPT_MINUTE_OI_ONLY_ENABLE
        try:
            self.mod.OPT_MINUTE_OI_ONLY_ENABLE = False
            self.assertIsNone(
                self.mod.szoMinuteRecordsToOiCandidate(records, asof)
            )
        finally:
            self.mod.OPT_MINUTE_OI_ONLY_ENABLE = saved

    def testListedTodayDailyDlEvenWithVolume(self):
        listed = ["90007069.SZO", "10012371.SHO", "10012372.SHO"]
        asof = date(2026, 8, 27)
        daily = {
            "10012371.SHO": {
                "d": asof,
                "close": 0.0523,
                "volume": 7402,
                "oi": 4448,
            },
            "10012372.SHO": {
                "d": asof,
                "close": 0.0232,
                "volume": 4832,
            },
        }
        out = self.mod.optionLiveListedDailyDownloadCodes(listed)
        self.assertEqual(out, ["10012371.SHO", "90007069.SZO", "10012372.SHO"])
        needStub = self.mod.optionCodesNeedingDailyDownload(listed, daily, asof)
        self.assertEqual(needStub, ["90007069.SZO"])

    def testListedTodayDailyDlEmpty(self):
        self.assertEqual(self.mod.optionLiveListedDailyDownloadCodes([]), [])
        self.assertEqual(self.mod.optionLiveListedDailyDownloadCodes(None), [])

    def testListedDailyChunkRotates(self):
        need = ["c%d" % i for i in range(5)]
        a, n1 = self.mod.optionListedDailyDownloadChunk(need, 0, 2)
        self.assertEqual(a, ["c0", "c1"])
        b, n2 = self.mod.optionListedDailyDownloadChunk(need, n1, 2)
        self.assertEqual(b, ["c2", "c3"])
        c, _n3 = self.mod.optionListedDailyDownloadChunk(need, n2, 2)
        self.assertEqual(c, ["c4", "c0"])
        self.assertEqual(
            self.mod.optionListedDailyDownloadChunk([], 0, 40),
            ([], 0),
        )

    def testShouldRefreshListedDailyStore(self):
        self.assertTrue(self.mod.shouldRefreshListedDailyStore(100.0, 0.0, 15.0))
        self.assertFalse(self.mod.shouldRefreshListedDailyStore(14.9, 0.0, 15.0))
        self.assertTrue(self.mod.shouldRefreshListedDailyStore(15.0, 0.0, 15.0))

    def testDailyDownloadStartYmdIsAsof(self):
        self.assertEqual(
            self.mod.optionDailyDownloadStartYmd(date(2026, 8, 27)),
            "20260827",
        )

    def testSzoQuoteSubDailyConfirmAndDelta(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 8, 26)
        wanted = ["10011516.SHO", "10011517.SHO"]
        owned = ["10011516.SHO", "10011517.SHO"]
        self.assertFalse(self.mod.shouldSyncOptionQuoteSub(asof, None, wanted, owned))
        self.assertTrue(
            self.mod.shouldSyncOptionQuoteSub(asof, dateCls(2026, 8, 25), wanted, owned)
        )
        self.assertFalse(self.mod.shouldSyncOptionQuoteSub(asof, asof, wanted, owned))
        self.assertTrue(
            self.mod.shouldSyncOptionQuoteSub(asof, asof, wanted, ["10011516.SHO"])
        )
        self.assertTrue(self.mod.optionQuoteSubIsHealthy(wanted, owned))
        self.assertFalse(self.mod.optionQuoteSubIsHealthy(wanted, ["10011516.SHO"]))
        # 想订 508 张而配额 500：订到配额上限即健康，否则永远判 unhealthy。
        fatWanted = ["10%06d.SHO" % i for i in range(508)]
        self.assertTrue(
            self.mod.optionQuoteSubIsHealthy(fatWanted, fatWanted[:500], 500)
        )
        self.assertFalse(
            self.mod.optionQuoteSubIsHealthy(fatWanted, fatWanted[:499], 500)
        )
        self.assertFalse(
            self.mod.shouldSyncOptionQuoteSub(asof, asof, fatWanted, fatWanted[:500], 500)
        )
        self.assertFalse(
            self.mod.shouldSyncOptionQuoteSub(
                asof, asof, wanted, owned, 500,
                lastSyncMono=10.0, nowMono=80.0, healSec=90.0,
            )
        )
        self.assertTrue(
            self.mod.shouldSyncOptionQuoteSub(
                asof, asof, wanted, owned, 500,
                lastSyncMono=10.0, nowMono=110.0, healSec=90.0,
            )
        )
        # callback 已归零时禁止 heal 的 drop/add；保留既有句柄，等缓存恢复。
        self.assertFalse(
            self.mod.shouldSyncOptionQuoteSub(
                asof, asof, wanted, owned, 500,
                lastSyncMono=10.0, nowMono=110.0, healSec=90.0,
                optionCallbackEmpty=True,
            )
        )
        self.assertTrue(
            self.mod.shouldSyncOptionQuoteSub(
                asof, asof, ["10011516.SHO"],
                ["10011516.SHO", "10012372.SHO"],
            )
        )
        self.assertEqual(
            self.mod.applySzoSubSyncLocal(
                [],
                {"kept": wanted, "added": [], "dropped": []},
            ),
            set(wanted),
        )

    def testPackOptionPollSlicesMergesSmallGroups(self):
        packed = self.mod.packOptionPollSlices(
            [list(range(94)), list(range(100, 204)), list(range(300, 490))],
            200,
        )
        self.assertEqual(len(packed), 2)
        self.assertEqual(len(packed[0]), 198)
        self.assertEqual(len(packed[1]), 190)

    def testGroupOptionCodesByUnderlying(self):
        meta = {
            "a.SHO": {"undl": "588000.SH"},
            "b.SHO": {"undl": "510300"},
            "c.SHO": {"undl": "588000"},
            "d.SZO": {"undl": "159915.SZ"},
        }
        groups = self.mod.groupOptionCodesByUnderlying(
            ["a.SHO", "b.SHO", "c.SHO", "d.SZO", "orphan.SHO"],
            meta,
        )
        self.assertEqual(groups[0], ["b.SHO"])
        self.assertEqual(groups[1], ["a.SHO", "c.SHO"])
        self.assertEqual(groups[2], ["d.SZO"])
        self.assertEqual(groups[3], ["orphan.SHO"])

    def testInterleaveFiveChainsForSubAndPoll(self):
        meta = {}
        codes = []
        for i in range(8):
            c050 = "050%02d.SHO" % i
            c300 = "300%02d.SHO" % i
            c500 = "500%02d.SHO" % i
            c000 = "000%02d.SHO" % i
            c915 = "915%02d.SZO" % i
            meta[c050] = {"undl": "510050.SH"}
            meta[c300] = {"undl": "510300.SH"}
            meta[c500] = {"undl": "510500.SH"}
            meta[c000] = {"undl": "588000.SH"}
            meta[c915] = {"undl": "159915.SZ"}
            codes.extend([c050, c300, c500, c000, c915])
        mixed = self.mod.interleaveOptionCodesByUnderlying(codes, meta)
        self.assertEqual(mixed[:5], [
            "05000.SHO", "30000.SHO", "50000.SHO", "00000.SHO", "91500.SZO",
        ])
        sub = self.mod.optionQuoteSubscribeCodes(codes, meta)
        self.assertTrue(all(c.endswith(".SHO") for c in sub))
        self.assertTrue(all(not c.startswith("050") for c in sub))
        self.assertEqual(sub[:3], [
            "30000.SHO", "50000.SHO", "00000.SHO",
        ])
        self.assertNotIn("91500.SZO", sub)
        self.assertNotIn("05000.SHO", sub)
        head = set(sub[:6])
        self.assertFalse(any(c.startswith("050") for c in sub))
        self.assertTrue(any(c.startswith("300") for c in head))
        self.assertTrue(any(c.startswith("500") for c in head))
        self.assertTrue(any(c.startswith("000") for c in head))
        self.assertEqual(
            self.mod.optionQuoteSubscribeCodes(["10011516.SHO", "90007075.SZO"]),
            [],
        )
        self.assertEqual(
            self.mod.optionQuoteSubscribeCodes(
                ["10011516.SHO", "90007075.SZO"],
                {"10011516.SHO": {"undl": "588000.SH"}},
            ),
            ["10011516.SHO"],
        )
        self.assertEqual(
            self.mod.interleaveTwo(["a", "b"], ["x"]),
            ["a", "x", "b"],
        )

    def testOptionCodesForUndlSix(self):
        meta = {
            "a.SHO": {"undl": "588000.SH"},
            "b.SHO": {"undl": "510300"},
            "c.SHO": {"undl": "588000"},
        }
        codes = ["b.SHO", "a.SHO", "c.SHO"]
        self.assertEqual(
            self.mod.optionCodesForUndlSix(codes, meta, ("588000",)),
            ["a.SHO", "c.SHO"],
        )

    def testRotateListAndPollSlicesKeepChain(self):
        self.assertEqual(self.mod.rotateList(["a", "b", "c"], 1), ["b", "c", "a"])
        meta = {"x.SHO": {"undl": "588000.SH"}}
        meta.update({"y%03d.SHO" % i: {"undl": "588000.SH"} for i in range(187)})
        codes = ["x.SHO"] + ["y%03d.SHO" % i for i in range(187)]
        slices = self.mod.optionPollSlices(codes, meta, 0)
        self.assertEqual(sum(len(s) for s in slices), 188)
        self.assertTrue(all(len(s) <= self.mod.OPT_CHUNK_SIZE for s in slices))
        self.assertEqual(len(slices), (188 + self.mod.OPT_CHUNK_SIZE - 1) // self.mod.OPT_CHUNK_SIZE)

    def testOnlyPollTodayTicks(self):
        from datetime import date as dateCls

        todayMs = datetime(2026, 8, 26, 10, 0).timestamp() * 1000.0
        oldMs = datetime(2026, 8, 21, 10, 0).timestamp() * 1000.0
        codes = ["live.SHO", "stale.SHO", "none.SHO"]
        tickMap = {
            "live.SHO": {"time": todayMs},
            "stale.SHO": {"time": oldMs},
        }
        live = self.mod.optionCodesWithTodayTicks(
            codes, tickMap, dateCls(2026, 8, 26),
        )
        self.assertEqual(live, ["live.SHO"])

    def testSkipMetaFillInSession(self):
        self.assertFalse(self.mod.shouldFillOptionMeta(datetime(2026, 8, 24, 10, 0, 0)))
        self.assertTrue(self.mod.shouldFillOptionMeta(datetime(2026, 8, 24, 15, 31, 0)))
        self.assertTrue(self.mod.shouldFillOptionMeta(datetime(2026, 8, 24, 8, 0, 0)))
        self.assertTrue(self.mod.shouldFillOptionMeta(datetime(2026, 8, 24, 15, 40, 0)))
        self.assertEqual(
            self.mod.optionMetaFillBatchSize(datetime(2026, 8, 24, 10, 0, 0)),
            self.mod.OPT_META_LIVE_GAP_BATCH,
        )
        self.assertEqual(
            self.mod.optionMetaFillBatchSize(datetime(2026, 8, 24, 8, 0, 0)),
            self.mod.OPT_META_BATCH,
        )
        self.assertEqual(self.mod.OPT_META_LIVE_GAP_BATCH, 8)
        self.assertTrue(self.mod.shouldFillOptionMetaInPoll())
        self.assertFalse(self.mod.shouldFillOptionMetaInPoll(yieldQuote=True))
        self.assertFalse(self.mod.shouldFillOptionMetaInPoll(skipHeavy=True))
        self.assertFalse(
            self.mod.shouldFillOptionMetaInPoll(
                deferHeavy=True, wholeQuoteOk=False,
            )
        )
        self.assertTrue(
            self.mod.shouldFillOptionMetaInPoll(
                deferHeavy=True, wholeQuoteOk=True,
            )
        )

    def testStartupChainZipWaitsThenStops(self):
        asof = date(2026, 8, 27)
        self.assertFalse(
            self.mod.shouldAttemptOptionChainZip(asof, None, 5.0, 8.0, 0.0)
        )
        self.assertTrue(
            self.mod.shouldAttemptOptionChainZip(asof, None, 8.0, 8.0, 0.0)
        )
        self.assertFalse(
            self.mod.shouldAttemptOptionChainZip(
                asof, asof, 20.0, 8.0, 0.0, chainComplete=True
            )
        )
        self.assertFalse(
            self.mod.shouldAttemptOptionChainZip(asof, None, 20.0, 8.0, 30.0)
        )
        self.assertTrue(
            self.mod.shouldAttemptOptionChainZip(
                asof, date(2026, 8, 26), 1.0, 8.0, 0.0
            )
        )
        self.assertTrue(
            self.mod.shouldAttemptOptionChainZip(
                asof, None, 100.0, 8.0, 0.0,
                chainComplete=False, lastZipMono=10.0, retrySec=90.0,
            )
        )
        self.assertFalse(
            self.mod.shouldAttemptOptionChainZip(
                asof, None, 80.0, 8.0, 0.0,
                chainComplete=False, lastZipMono=10.0, retrySec=90.0,
            )
        )

    def testThrottleDailyBarsInSession(self):
        self.assertFalse(self.mod.shouldRefreshDailyBars(20.0, 10.0, 15.0, True))
        self.assertTrue(self.mod.shouldRefreshDailyBars(26.0, 10.0, 15.0, True))
        self.assertTrue(self.mod.shouldRefreshDailyBars(11.0, 10.0, 15.0, False))

    def testEtfContinuousSkipsRtk(self):
        self.assertTrue(self.mod.isEtfContinuousSession(datetime(2026, 8, 25, 9, 15)))
        self.assertTrue(self.mod.isEtfContinuousSession(datetime(2026, 8, 25, 14, 59)))
        self.assertFalse(self.mod.isEtfContinuousSession(datetime(2026, 8, 25, 15, 0)))
        self.assertFalse(self.mod.isEtfContinuousSession(datetime(2026, 8, 25, 9, 14)))

    def testEtfDailySkippedWhenWholeQuoteLive(self):
        asof = datetime(2026, 8, 25).date()
        live = {
            code: {"lastPrice": 1.5, "time": None}
            for code in self.mod.OPT_UNDERLYING_QMT
        }
        self.assertTrue(self.mod.etfHasLiveSessionSpot(live, asof))
        self.assertFalse(self.mod.etfHasLiveSessionSpot({}, asof))
        self.assertFalse(self.mod.shouldRefreshEtfDailyBars(True, True))
        self.assertTrue(self.mod.shouldRefreshEtfDailyBars(True, False))
        self.assertTrue(self.mod.shouldRefreshEtfDailyBars(False, True))

    def testPollBusyContractsFirst(self):
        codes = ["thin.SHO", "hot.SHO", "mid.SHO"]
        ticks = {"hot.SHO": {"vol": 9000}, "mid.SHO": {"vol": 100}}
        daily = {"thin.SHO": {"volume": 1}}
        out = self.mod.orderOptionCodesForPoll(codes, ticks, daily)
        self.assertEqual(out, ["hot.SHO", "mid.SHO", "thin.SHO"])

    def testPollAdjustedGoesLast(self):
        codes = ["adjA.SHO", "hot.SHO", "adjUnit.SHO"]
        ticks = {
            "adjA.SHO": {"vol": 99999},
            "hot.SHO": {"vol": 10},
            "adjUnit.SHO": {"vol": 88888},
        }
        out = self.mod.orderOptionCodesForPoll(
            codes,
            ticks,
            {},
            adjustedSet={"adjA.SHO"},
            metaMap={"adjUnit.SHO": {"unit": 10075}},
        )
        self.assertEqual(out[0], "hot.SHO")
        self.assertEqual(out[1], "adjA.SHO")
        self.assertEqual(out[2], "adjUnit.SHO")


class ScriptSafetyTest(unittest.TestCase):
    def testNoFullTickPrimeOrWritePath(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("prime=True", text)
        self.assertIn("prime=False", text)
        self.assertIn("probeEtfWholeQuoteTicks", text)
        self.assertIn("get_full_tick", text)
        self.assertNotIn("get_full_tick(['SH'", text)
        self.assertNotIn('get_full_tick(["SH"', text)
        self.assertNotIn("get_full_tick(['SZ'", text)
        self.assertIn("force=True", text)
        self.assertIn("OPT_YIELD whole_quote", text)
        self.assertIn("SUBSCRIBE_NOOP", text)
        self.assertIn("SUBSCRIBE_NOOP_BACKOFF_SEC", text)
        self.assertIn("shouldEmitNamedLog", text)
        self.assertIn("OPT_FAIL_BACKOFF_SEC", text)
        self.assertIn("shouldDeferOptionHeavyRpc", text)
        self.assertIn("OPT_QUOTE_SUB_UNDLS", text)
        self.assertIn("OPT_YIELD_SILENCE_SEC", text)
        self.assertIn("OPT_LIVE_TICK_MAX_CHUNKS", text)
        self.assertIn("markOptRpcBusy", text)
        self.assertIn("假活先让路", text)
        self.assertIn("skip tick/1m", text)
        self.assertIn("rpcFallback=False", text)
        self.assertIn("OPT_SZO_MIN_LOCAL", text)
        self.assertIn("SUBSCRIBE_GRACE_SEC = 20.0", text)
        self.assertIn("ETF_PROBE_LIVE_SEC = 3.0", text)
        self.assertIn("not yieldQuote", text)
        self.assertNotIn("to_sql", text)
        self.assertNotIn("INSERT INTO", text)
        self.assertNotIn("REPLACE INTO", text)
        self.assertIn("get_option_ticks", text)
        self.assertIn("get_option_undl_data", text)
        self.assertIn("get_option_detail_data", text)
        self.assertIn("optMeta", text)
        self.assertIn("opt_basic", text)
        self.assertNotIn("OPT_PROBE", text)
        self.assertIn("OPT_CHUNK_SIZE = 20", text)
        self.assertIn("OPT_POLL_FAIL_ABORT = 2", text)
        self.assertIn("OPT_SZO_SUB_ADD_LIMIT = 40", text)
        self.assertIn("OPT_ROUND defer", text)
        self.assertIn("skip tick/1m", text)
        self.assertIn("optionDailyLastAllowed", text)
        self.assertIn("optionPremiumLooksImplausible", text)
        self.assertIn("scaleTenXOptLast", text)
        self.assertIn("shouldApplySzoMinuteTick", text)
        self.assertIn("isOptionCatchupWindow", text)
        self.assertIn("OPT_CATCHUP_CHUNKS", text)
        self.assertIn("OPT_QUOTE_CACHE_CATCHUP_AGE_SEC", text)
        self.assertIn("OPT_CATCHUP", text)
        self.assertIn("optionSzoMinuteNeedCodes", text)
        self.assertIn("optionSzoMinuteMarkFailed", text)
        self.assertIn("optionLivePollCodes", text)
        self.assertIn("OPT_SUB force resync", text)
        self.assertIn("OPT_QUOTE_SUB_HEAL_SEC", text)
        self.assertIn("OPT_QUOTE_SUB_FORCE_COOLDOWN_SEC", text)
        self.assertIn("isOptionContinuousAuction", text)
        self.assertIn('payload["force"] = True', text)
        self.assertIn("shouldSkipLiveDailyBars", text)
        self.assertIn("OPT_DAILY_LIVE_SEC = 10.0", text)
        liveWin = text.split("if inWindow:", 2)[-1]
        tickAt = liveWin.find("_pollOptionRound")
        dailyAt = liveWin.find("dailyN = _refreshOptionDailyWithGates")
        self.assertGreaterEqual(tickAt, 0)
        self.assertGreaterEqual(dailyAt, 0)
        self.assertLess(tickAt, dailyAt)
        self.assertGreater(tickAt, liveWin.find("OPT_LIVE_TICK_MAX_CHUNKS"))
        self.assertIn("optionDailySubscribeCodes", text)
        self.assertIn("optionTickIsFresh", text)
        self.assertIn("OPT_DAILY_WITH_TICK_SEC = 90.0", text)
        self.assertIn("OPT_CHUNK_YIELD_SEC = 0.4", text)
        self.assertIn("shouldTreatAsFakeAlive", text)
        self.assertIn("tryRpcPingMs", text)
        self.assertIn("PING_FAKE_ALIVE_MAX_MS", text)
        self.assertIn("shouldRefreshEtfDailyBars", text)
        self.assertIn("optionDailyBarLooksStub", text)
        self.assertIn("OPT_DAILY_DL", text)
        self.assertIn("optionCodesForUndlSix", text)
        self.assertIn("shouldFillOptionMeta", text)
        self.assertIn("shouldFillOptionMetaInPoll", text)
        self.assertIn("optionMetaPreferCodes", text)
        self.assertIn("preferCodes=added", text)
        self.assertIn("直播窗窄口补 optMeta", text)
        self.assertIn("optionMetaFillBatchSize(now)", text)
        self.assertIn("shouldAttemptOptionChainZip", text)
        self.assertIn("OPT_CHAIN_ZIP_DELAY_SEC = 4.0", text)
        self.assertIn("OPT_META_LIVE_GAP_BATCH = 8", text)
        self.assertIn("OPT_CHAIN_INCOMPLETE_RETRY_SEC", text)
        self.assertIn("optionMetaNeedCodes", text)
        self.assertIn("OPT_LISTED_LOOKBACK_TRADE_DAYS", text)
        self.assertIn("optionListedLookbackStart", text)
        self.assertIn("optionLivePollWaitSec", text)
        self.assertIn("optionDailyPollCodes", text)
        self.assertIn("optionStubDailyDownloadCodes", text)
        self.assertIn("szoSubscribeInProgress", text)
        self.assertIn("optionDailyGateMode", text)
        self.assertIn("OPT_DAILY_GATE", text)
        self.assertIn("订全推后跳过 QMT 拉链", text)
        self.assertIn("seedOptionMetaFromOptBasic", text)
        self.assertIn("groupOptionCodesByUnderlying", text)
        self.assertIn("interleaveOptionCodesByUnderlying", text)
        self.assertIn("optionPollSlices", text)
        self.assertIn("packOptionPollSlices", text)
        self.assertIn("sync_single_quote", text)
        self.assertIn("OPT_SZO_SUB_PERIOD", text)
        self.assertIn("OPT_QUOTE_SUB_MAX_OWNED", text)
        self.assertIn("get_single_quote_ticks", text)
        self.assertIn("OPT_SZO_MIN_PERIOD", text)
        self.assertIn("szoMinuteRecordsToTick", text)
        self.assertIn("optionCodesWithTodayTicks", text)
        self.assertIn("_rpcLock", text)
        self.assertIn("PING_WATCHDOG_TIMEOUT_SEC = 12.0", text)
        self.assertIn("SNAP_JSON_TTL_SEC = 0.4", text)
        self.assertIn("OPT_DAILY_IDLE_SEC = 10.0", text)
        self.assertIn("encodeQuoteSnapshotJson", text)
        self.assertIn("stealPreviousQuoteInstance", text)
        self.assertIn("enforceSoleQuoteInstance", text)
        self.assertIn("STALE_UNIQUE", text)
        self.assertIn("QUOTE_PEER_CONFLICT", text)
        self.assertIn("15580 被占用", text)
        self.assertIn("DAQMT_QUOTE_STEAL_LOCK", text)
        self.assertIn("STEAL_SETTLE_SEC = 2.5", text)
        self.assertIn("waitUntilRpcPing", text)
        self.assertIn("EXPECTED_RPC_REVISION", text)
        self.assertIn("OPT_HEALTH dead_push", text)
        self.assertIn("revision_mismatch", text)
        self.assertIn("subscribeWholeQuoteUntilOk", text)
        self.assertIn("skipPing=True", text)
        self.assertNotIn('logger.error("订阅失败:', text)
        self.assertIn("XtItClient", text)
        self.assertIn("/snapshot", text)
        self.assertIn("etfTicks", text)

    def testOptionPollLoopWiresOiGateBeforeNonCriticalRpc(self):
        """OI压力门必须在 live tick/OPT_CHUNK 与要素补齐之前生效。"""
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        gateAt = text.index("oiGate = optionChunkOiGateDecision(")
        liveAt = text.index("_maybeDownloadLiveStaleOptionTicks(", gateAt)
        chunkAt = text.index("_pollOptionRound(", gateAt)
        metaAt = text.index("_fillOptionMetaGaps(", gateAt)
        self.assertLess(gateAt, liveAt)
        self.assertLess(gateAt, chunkAt)
        self.assertLess(gateAt, metaAt)
        self.assertIn('if oiGate["defer"] or oiGate["probe"]:', text[gateAt:chunkAt])
        self.assertIn('bool(oiGate["defer"] or oiGate["probe"])', text[gateAt:metaAt])


def _liveQmtTickOptionCount(mod) -> int:
    """独立 SQL：QMT tick 三链（OPT_QMT_TICK_UNDERLYINGS）当前在市 C/P 合约张数。"""
    import pymysql

    names = tuple(mod.OPT_QMT_TICK_UNDERLYINGS)
    conn = pymysql.connect(**mod.tushareDbConfig())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT ts_code) FROM opt_basic WHERE delist_date >= CURDATE() "
                "AND UPPER(TRIM(call_put)) IN ('C','P') AND ("
                + " OR ".join(["opt_code LIKE %s"] * len(names)) + ")",
                ["%" + n + "%" for n in names],
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


class OptBasicLoadTest(unittest.TestCase):
    def testListedOnQueryUsesListDate(self):
        mod = _loadMod()
        sql, params = mod.optBasicListedOnQuery(date(2026, 8, 27))
        self.assertIn("list_date >= %s", sql)
        self.assertIn("list_date <= %s", sql)
        self.assertIn("delist_date >= CURDATE()", sql)
        self.assertEqual(params[0], date(2026, 8, 17))
        self.assertEqual(params[1], date(2026, 8, 27))
        self.assertTrue(any("%588000%" in str(p) for p in params))
        sql0, params0 = mod.optBasicListedOnQuery(
            date(2026, 8, 27), startDate=date(2026, 8, 27)
        )
        self.assertEqual(params0[0], date(2026, 8, 27))
        self.assertEqual(params0[1], date(2026, 8, 27))

    def testLiveMappedCount(self):
        """QMT 期权宇宙 = 510300/510500/588000（约 422 张，即订阅配额内数量）。

        2026-09-11：510050（无订阅配额）与 159915（深市 .SZO 无 tick 回调）
        改由通达信供给，故本宇宙**只剩 .SHO**，不再含 .SZO。
        """
        mod = _loadMod()
        codes = mod.loadEtfOptionQmtCodes()
        # 在市张数随到期/加挂周期涨落（9/11 约 422，9 月合约到期后 356）：与库内独立计数对账，不写死常数
        self.assertEqual(len(codes), _liveQmtTickOptionCount(mod))
        self.assertGreaterEqual(len(codes), 200)
        self.assertTrue(all(c.endswith(".SHO") for c in codes))
        self.assertTrue(any(c.endswith(".SHO") for c in codes))

    def testLiveOptionMetaFromOptBasic(self):
        """期权要素映射覆盖 QMT tick 宇宙（510300/510500/588000）。

        2026-09-11：510050 改由通达信供给，不再进 QMT 期权管线，
        故条数从五链（≈648）降为三链（≈422，即订阅配额内数量）。
        """
        mod = _loadMod()
        mapping = mod.loadOptionMetaFromOptBasic()
        self.assertEqual(len(mapping), _liveQmtTickOptionCount(mod))
        self.assertGreaterEqual(len(mapping), 200)
        self.assertTrue(all(str(v.get("undl") or "").startswith(("510300", "510500", "588000"))
                            for v in mapping.values()),
                        "QMT 期权宇宙不应包含 510050/159915")
        sample = next(iter(mapping.values()))
        self.assertIn("k", sample)
        self.assertIn("undl", sample)
        self.assertIn("cp", sample)

    def testNewerIncomingTickNotTreatedAsStale(self):
        """回归（2026-09-11）：tick 比记录**新**时绝不能判为陈旧。

        现场：`existing.time=13:22:34`、`tick.time=13:28:48`（新 373s）被
        旧实现的 `abs(refMs-ms)` 判成陈旧 → 更新丢弃 → 时钟永不推进，
        510500 有 12 张活跃合约（成交量 500~4600）长期显示陈旧。
        """
        from datetime import datetime, timedelta

        m = _loadMod()
        base = datetime.now().timestamp() * 1000.0
        existing = {
            "time": base - timedelta(seconds=373).total_seconds() * 1000.0,
            "fromTdx": True,
            "last": 0.0013,
            "vol": 1558.0,
        }
        # 比 existing 新 → 不是回盖
        self.assertFalse(m.isStaleIncomingTick(base, existing))
        # 比 existing 旧超过阈值(默认 90s) → 是回盖
        refMs = m.optionTickTimeMs(existing)
        muchOlder = refMs - 200_000.0
        self.assertTrue(m.isStaleIncomingTick(muchOlder, existing))
        # 只旧 30s（阈值内）→ 不算回盖
        self.assertFalse(m.isStaleIncomingTick(refMs - 30_000.0, existing))

    def testFreshObservationAdvancesClockWithoutValueChange(self):
        """回归（2026-09-11）：源端新鲜、值未变，也要推进字段时钟。

        否则「值长期不变」的活跃合约会被永久判为陈旧（旧实现只在值变化时写补丁）。
        """
        from datetime import datetime, timedelta

        m = _loadMod()
        now = datetime.now().timestamp() * 1000.0
        oldBook = now - timedelta(seconds=300).total_seconds() * 1000.0
        existing = {
            "time": oldBook,
            "fromTdx": True,
            "last": 0.0013,
            "vol": 1558.0,
            "oi": 11634.0,
            "bid1": 0.0012,
        }
        incoming = {
            "time": now,
            "lastPrice": 0.0013,   # 与 existing 相同
            "volume": 1558.0,      # 相同
            "openInt": 11634.0,    # 相同
        }
        patched = m.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched, "值未变但时钟应推进，不应返回 None")
        self.assertEqual(
            m.optionTickFieldTimeMs(patched, "qty"), now
        )
        self.assertEqual(
            m.optionTickFieldTimeMs(patched, "last"), now
        )
        # 值仍保持
        self.assertEqual(patched.get("vol"), 1558.0)
        self.assertEqual(patched.get("last"), 0.0013)

    def testForceResyncOnPushDegradation(self):
        """回归（2026-09-11）：推送「速率退化」（非零但极低）也必须触发 force 重订。

        现场：午休→下午盘边界后 `OPT_QUOTE_CACHE hit` 由 ~140 掉到 16~48
        （约 1/6），持续约 10 分钟，T 板整列变灰。
        旧实现 `shouldForceQuoteSubResync` 只在 `cacheHitN == 0` 时返回 True，
        且调用点把评估放在 `else`（cached 为空）分支里 —— **退化场景永不重订**。
        """
        m = _loadMod()
        common = dict(isSubFull=True, inAuction=True, sinceForceSec=999.0)
        # 健康命中率 → 不重订
        self.assertFalse(m.shouldForceQuoteSubResync(
            140, 0, cachedN=421, degradedStreak=5, **common))
        # 退化（1/6）但连续次数不足 → 不重订
        self.assertFalse(m.shouldForceQuoteSubResync(
            25, 0, cachedN=421, degradedStreak=2, **common))
        # 退化且连续达标 → 重订
        self.assertTrue(m.shouldForceQuoteSubResync(
            25, 0, cachedN=421, degradedStreak=3, **common))
        # 完全空缓存的老路径仍然有效
        self.assertTrue(m.shouldForceQuoteSubResync(
            0, 3, cachedN=421, degradedStreak=0, **common))
        # 非竞价时段一律不重订（午休空缓存是常态）
        self.assertFalse(m.shouldForceQuoteSubResync(
            0, 9, cachedN=421, degradedStreak=9,
            isSubFull=True, inAuction=False, sinceForceSec=999.0))
        # 冷却期内不重订
        self.assertFalse(m.shouldForceQuoteSubResync(
            25, 0, cachedN=421, degradedStreak=9,
            isSubFull=True, inAuction=True, sinceForceSec=10.0,
            forceCooldownSec=90.0))

    def testQuoteCachePollOutcomeSeparatesEmptyFromTransportFailures(self):
        """瞬时 busy/timeout/error/无方法不能伪装成权威空缓存。"""
        m = _loadMod()

        class Logger:
            def info(self, *_args, **_kwargs):
                return None

        class Trader:
            def __init__(self, getter=None):
                if getter is not None:
                    self.get_single_quote_ticks = getter

        def raiseBusy(**_kwargs):
            raise RuntimeError("RPC busy")

        def raiseTimeout(**_kwargs):
            raise TimeoutError("timed out")

        def raiseError(**_kwargs):
            raise RuntimeError("boom")

        cases = [
            (Trader(lambda **_kwargs: {}), "authoritative_empty"),
            (Trader(lambda **_kwargs: {"10000001.SHO": {"time": 1}}),
             "authoritative_empty"),
            (Trader(raiseBusy), "busy"),
            (Trader(raiseTimeout), "timeout"),
            (Trader(raiseError), "error"),
            (Trader(), "transport_unknown"),
        ]
        for trader, expected in cases:
            with self.subTest(expected=expected):
                data, outcome = m._pollOptionQuoteCache(
                    Logger(), trader, returnOutcome=True,
                )
                self.assertEqual(data, {})
                self.assertEqual(outcome, expected)

    def testTransientCacheEmptyAfterQtyDoesNotCountOrForce(self):
        """OPT_QTY 刚结束后的权威空读只复查，不累计 dead_push。"""
        m = _loadMod()
        self.assertFalse(m.shouldCountOptionQuoteCacheEmpty(
            "authoritative_empty", True, sinceQtySec=0.8,
        ))
        self.assertTrue(m.shouldCountOptionQuoteCacheEmpty(
            "authoritative_empty", True, sinceQtySec=2.1,
        ))
        self.assertTrue(m.shouldDeferOptionQuoteCacheRecheck(
            "authoritative_empty", True, sinceQtySec=0.8,
        ))
        self.assertFalse(m.shouldDeferOptionQuoteCacheRecheck(
            "authoritative_empty", True, sinceQtySec=2.1,
        ))
        for outcome in ("busy", "timeout", "error", "transport_unknown"):
            self.assertFalse(m.shouldCountOptionQuoteCacheEmpty(
                outcome, True, sinceQtySec=99.0,
            ))
        self.assertFalse(m.shouldForceQuoteSubResync(
            0, 3, isSubFull=True, inAuction=True,
            cacheAuthoritative=True, healthDegraded=False,
        ))
        self.assertTrue(m.shouldForceQuoteSubResync(
            0, 3, isSubFull=True, inAuction=True,
            cacheAuthoritative=True, healthDegraded=True,
        ))

    def testOptionPollLoopWiresCacheRecheckAheadOfHeavyRpc(self):
        """复查 pending 必须同时挡住 OPT_QTY 与订阅 heal，避免宽限被反复刷新。"""
        src = inspect.getsource(_loadMod()._runOptionPollLoop)
        self.assertGreaterEqual(src.count("or quoteCacheRecheckPending"), 2)
        self.assertIn("cacheRetrySec = 0.5 if quoteCacheRecheckPending", src)
        self.assertIn("shouldDeferOptionQuoteCacheRecheck", src)

    def testCallbackReceiptCannotMaskStaleMarketEvent(self):
        """桥接重复回调旧 tick 时，外层必须把它按零命中处理以触发重订。"""
        from datetime import timedelta

        m = _loadMod()
        now = datetime.now()
        fresh = now.timestamp() * 1000.0
        stale = (now - timedelta(seconds=300)).timestamp() * 1000.0
        kept, staleN = m.filterOptionQuoteCacheTicks(
            {
                "10000001.SHO": {"time": fresh, "openInt": 1},
                "10000002.SHO": {"time": stale, "openInt": 2},
            },
            now=now,
            maxAgeSec=30.0,
        )
        self.assertEqual(set(kept), {"10000001.SHO"})
        self.assertEqual(staleN, 1)

    def testDegradeDetectorEvaluatedOutsideEmptyBranch(self):
        """退化评估必须在 `if cached:` 之外，否则非零退化场景永不被评估。"""
        import inspect

        m = _loadMod()
        src = inspect.getsource(m)
        self.assertIn("subDegradeStreak", src)
        self.assertIn("OPT_SUB_DEGRADE_RATIO", src)
        # 关键：退化统计不得嵌在 `elif not inAuction:` 的 else 分支内
        idxDeg = src.index("subDegradeStreak += 1")
        idxBranch = src.rindex("if canPollCache:", 0, idxDeg)
        seg = src[idxBranch:idxDeg]
        self.assertNotIn("elif not inAuction:", seg.replace(
            "elif not inAuction:\n                        quoteCacheEmptyHits = 0", ""))
        # 调用点必须传 degradedStreak
        self.assertIn("degradedStreak=subDegradeStreak", src)

    def testQmtLedChainYieldsWhenQmtStale(self):
        """回归（2026-09-11 下午盘）：QMT 期权源冻结时必须让 TDX 接管。

        现场：13:05 QMT 订阅/轮询期权 tick 卡在 13:02:48（滞后 155s），
        而 TDX 仅 2~35s；旧逻辑只看时段，导致 daqmt 快照 age≈5144s、
        T 板整链灰色（如实反映陈旧）。
        """
        from datetime import datetime, timedelta
        from unittest.mock import patch

        m = _loadMod()
        nowMs = datetime.now().timestamp() * 1000.0
        fresh = {"time": nowMs, "last": 0.1, "vol": 10}
        stale = {
            "time": (datetime.now() - timedelta(seconds=155)).timestamp() * 1000.0,
            "last": 0.1,
            "vol": 10,
        }
        with patch.object(m, "isWatchdogWindow", lambda *a, **k: True):
            # QMT 新鲜 → 主导，TDX 不覆盖
            self.assertTrue(m.qmtLedChain("510500", fresh))
            self.assertFalse(
                m.tdxOptCodeAllowed("10012399.SHO", {"undl": "510500"}, None, fresh)
            )
            # QMT 陈旧 → 让位，TDX 放行（降级/自愈）
            self.assertFalse(m.qmtLedChain("510500", stale))
            self.assertTrue(
                m.tdxOptCodeAllowed("10012399.SHO", {"undl": "510500"}, None, stale)
            )
            # 无 existing（尚未有 QMT tick）保持主导，由 QMT 先建仓
            self.assertTrue(m.qmtLedChain("510500", None))

    def testQmtTickUniverseExcludesTdxChains(self):
        """510050 / 159915 不进 QMT 期权 tick 宇宙（由通达信供给）。"""
        mod = _loadMod()
        self.assertEqual(mod.OPT_QMT_TICK_UNDERLYINGS, ("510300", "510500", "588000"))
        codes = mod.optUnderlyingQmtCodes(mod.OPT_QMT_TICK_UNDERLYINGS)
        self.assertNotIn("510050.SH", codes)
        self.assertNotIn("159915.SZ", codes)
        self.assertIn("510500.SH", codes)


class OptionChainResolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testUnderlyingMarketSuffix(self):
        codes = self.mod.optUnderlyingQmtCodes()
        self.assertIn("510050.SH", codes)
        self.assertIn("159915.SZ", codes)
        self.assertNotIn("159915.SH", codes)

    def testExtractFromListAndDict(self):
        extract = self.mod.extractOptionCodesFromUndlData
        self.assertEqual(
            extract(["10011485.SHO", "90007481.SZO", "588000.SH"]),
            ["10011485.SHO", "90007481.SZO"],
        )
        self.assertEqual(
            extract([{"code": "10011485.SH"}, {"option_code": "90007481.SZ"}]),
            ["10011485.SHO", "90007481.SZO"],
        )
        self.assertEqual(
            extract({"510050.SH": ["10011485.SHO"]}),
            ["10011485.SHO"],
        )

    def testFloorRejectsMissingLeg(self):
        okPer = {
            "510050.SH": 94,
            "510300.SH": 104,
            "510500.SH": 190,
            "588000.SH": 188,
            "159915.SZ": 152,
        }
        self.assertTrue(self.mod.optionChainMeetsFloor(okPer))
        bad = dict(okPer)
        bad["159915.SZ"] = 0
        self.assertFalse(self.mod.optionChainMeetsFloor(bad))
        self.assertFalse(self.mod.optionChainMeetsFloor({}))

    def testRefreshOnlyOnDateChange(self):
        from datetime import date as dateCls

        self.assertTrue(
            self.mod.shouldRefreshOptionChain(dateCls(2026, 8, 26), dateCls(2026, 8, 25))
        )
        self.assertFalse(
            self.mod.shouldRefreshOptionChain(dateCls(2026, 8, 25), dateCls(2026, 8, 25))
        )
        self.assertTrue(self.mod.shouldRefreshOptionChain(dateCls(2026, 8, 25), None))

    def testChainDeltaAndChunkFollowN(self):
        added, dropped = self.mod.optionChainDelta(["a.SHO", "b.SHO"], ["b.SHO", "c.SHO"])
        self.assertEqual(added, ["c.SHO"])
        self.assertEqual(dropped, ["a.SHO"])
        self.assertEqual(len(self.mod.chunkCodes(list(range(900)), 150)), 6)
        self.assertEqual(len(self.mod.chunkCodes(list(range(600)), 150)), 4)

    def testOptionUniverseMergeKeepsTdxFallbackAfterQmtResolve(self):
        """启动后 QMT 三链结果不能覆盖已经准备好的 TDX 五链。"""
        mod = self.mod
        self.assertEqual(
            mod.mergeOptionUniverseCodes(
                ["10030001.SHO", "10030002.SHO"],
                ["10030002.SHO", "10050001.SHO", "90091501.SZO"],
            ),
            ["10030001.SHO", "10030002.SHO", "10050001.SHO", "90091501.SZO"],
        )

    def testRefreshOptionChainPreservesTdxCodes(self):
        """日切重拉 QMT 链时仍保留 510050/159915 的 TDX 调度代码。"""
        from unittest.mock import patch
        import logging

        mod = self.mod
        oldCodes = ["10030001.SHO", "10050001.SHO", "90091501.SZO"]
        meta = {
            "10050001.SHO": {"undl": "510050", "k": 2.5},
            "90091501.SZO": {"undl": "159915", "k": 2.5},
        }
        with (
            patch.object(
                mod,
                "loadEtfOptionCodesFromQmt",
                return_value=(
                    ["10030002.SHO"],
                    {"510300.SH": 1, "510500.SH": 1, "588000.SH": 1},
                ),
            ),
            patch.object(mod, "optionChainMeetsFloor", return_value=True),
            patch.object(mod, "_copyOptionMeta", return_value=meta),
            patch.object(mod, "pruneOptionCaches"),
            patch.object(mod, "rememberOptionChainAdds"),
            patch.object(mod, "_seedListedTodayFromOptBasic"),
            patch.object(mod, "reloadAdjustedOptionCodes"),
            patch.object(mod, "_fillOptionMetaGaps"),
        ):
            got = mod._tryRefreshOptionChain(
                logging.getLogger("option-chain-test"), object(), oldCodes
            )
        self.assertEqual(got, ["10030002.SHO", "10050001.SHO", "90091501.SZO"])

    def testPruneDroppedContracts(self):
        self.mod.optionTicks.clear()
        self.mod.optionDaily.clear()
        self.mod.optionTicks["old.SHO"] = {"last": 1}
        self.mod.optionTicks["keep.SHO"] = {"last": 2}
        self.mod.optionDaily["old.SHO"] = {"volume": 1}
        n = self.mod.pruneOptionCaches(["keep.SHO"])
        self.assertEqual(n, 2)
        self.assertNotIn("old.SHO", self.mod.optionTicks)
        self.assertIn("keep.SHO", self.mod.optionTicks)
        self.mod.optionTicks.clear()
        self.mod.optionDaily.clear()
        self.mod.optionMeta.clear()

    def testCompactOptionMetaCallPutExpireInt(self):
        call = self.mod.compactOptionMeta(
            "10012153.SHO",
            {
                "optType": "CALL",
                "OptExercisePrice": 1.7,
                "ExpireDate": 20260826,
                "EndDelivDate": 20260826,
                "OptUndlCode": "588000",
                "OptUnit": 10000,
                "VolumeMultiple": 10000,
            },
        )
        self.assertEqual(call["k"], 1.7)
        self.assertEqual(call["exp"], "2026-08-26")
        self.assertEqual(call["sm"], "202608")
        self.assertEqual(call["cp"], "C")
        self.assertEqual(call["unit"], 10000.0)
        self.assertEqual(call["undl"], "588000")
        put = self.mod.compactOptionMeta(
            "10012154.SHO",
            {
                "optType": "PUT",
                "OptExercisePrice": 1.7,
                "ExpireDate": 20260826,
                "OptUndlCode": "588000",
                "VolumeMultiple": 10000,
            },
        )
        self.assertEqual(put["cp"], "P")
        szo = self.mod.compactOptionMeta(
            "90007784.SZO",
            {
                "optType": "PUT",
                "OptExercisePrice": 2.5,
                "ExpireDate": 20260923,
                "OptUndlCode": "159915",
                "OptUnit": 10000,
            },
        )
        self.assertEqual(szo["undl"], "159915")
        self.assertEqual(szo["cp"], "P")
        self.assertEqual(self.mod.parseQmtExpireDate(20260826), date(2026, 8, 26))
        byType = self.mod.compactOptionMeta(
            "10013001.SHO",
            {
                "ProductType": 0,
                "OptExercisePrice": 1.8,
                "ExpireDate": 20261028,
                "OptUndlCode": "588000.SH",
                "OptUnit": 10000,
            },
        )
        self.assertEqual(byType["cp"], "C")
        self.assertEqual(byType["sm"], "202610")
        self.assertEqual(byType["undl"], "588000.SH")
        self.assertEqual(
            self.mod.optionCallPutFromDetail({"ProductType": 1}),
            "P",
        )

    def testCompactOptionMetaRejectsBad(self):
        self.assertIsNone(self.mod.compactOptionMeta("10012153.SHO", {}))
        self.assertIsNone(self.mod.compactOptionMeta("", {"optType": "CALL"}))

    def testUndlFromOptBasicOptCode(self):
        self.assertEqual(self.mod.undlFromOptBasicOptCode("OP510300.SH"), "510300.SH")
        self.assertEqual(self.mod.undlFromOptBasicOptCode("OP159915.SZ"), "159915.SZ")
        self.assertEqual(self.mod.undlFromOptBasicOptCode("510500.SH"), "510500.SH")

    def testCompactOptionMetaFromBasicRow(self):
        packed = self.mod.compactOptionMetaFromBasicRow(
            "10011459.SH", "P", 5.25, date(2026, 12, 23), "10000.0", "OP510300.SH",
        )
        self.assertIsNotNone(packed)
        qmt, meta = packed
        self.assertEqual(qmt, "10011459.SHO")
        self.assertEqual(meta["cp"], "P")
        self.assertEqual(meta["k"], 5.25)
        self.assertEqual(meta["exp"], "2026-12-23")
        self.assertEqual(meta["undl"], "510300.SH")
        self.assertEqual(meta["unit"], 10000.0)

    def testPollChunkSplitsThenOk(self):
        class Xd:
            n = 0

            def get_option_ticks(self, chunk, count=1, chunk_size=0, timeout_seconds=None):
                self.n += 1
                if len(chunk) >= 80:
                    raise RuntimeError("zmq rpc timeout: get_market_data_ex")
                return {code: {"lastPrice": 1.0} for code in chunk}

        import logging

        codes = ["c%03d.SHO" % i for i in range(150)]
        xd = Xd()
        try:
            ok, fail, _ms = self.mod._pollOneOptionChunk(
                logging.getLogger("t"), xd, codes, tuple(codes[:3]), 0,
            )
            self.assertEqual(ok, 150)
            self.assertEqual(fail, 0)
            # 150 超时拆成 75+75；mock 仅 ≥80 失败 → 1 次失败 + 2 次成功
            self.assertEqual(xd.n, 3)
        finally:
            self.mod.optionTicks.clear()
            self.mod.optionStats["uniqueCodes"].clear()

    def testPollChunkSplitOnTimeoutSize20(self):
        """直播窗片宽 20 超时必须能对半，否则整片白烧。"""
        class Xd:
            n = 0

            def get_option_ticks(self, chunk, count=1, chunk_size=0, timeout_seconds=None):
                self.n += 1
                if len(chunk) >= 20:
                    raise RuntimeError("zmq rpc timeout: get_market_data_ex")
                return {code: {"lastPrice": 1.0} for code in chunk}

        import logging

        codes = ["c%02d.SHO" % i for i in range(20)]
        xd = Xd()
        try:
            ok, fail, _ms = self.mod._pollOneOptionChunk(
                logging.getLogger("t"), xd, codes, tuple(codes[:3]), 0,
            )
            self.assertEqual(ok, 20)
            self.assertEqual(fail, 0)
            # 1 次失败(20) + 2 次成功(10)
            self.assertEqual(xd.n, 3)
        finally:
            self.mod.optionTicks.clear()
            self.mod.optionStats["uniqueCodes"].clear()

    def testOptChunkPressureParams(self):
        self.mod._optChunkPressureUntil = 0.0
        normal = self.mod.optPollChunkParams(False)
        self.assertEqual(normal["chunkSize"], self.mod.OPT_CHUNK_SIZE)
        self.assertEqual(normal["maxChunks"], self.mod.OPT_LIVE_TICK_MAX_CHUNKS)
        pressed = self.mod.optPollChunkParams(True)
        self.assertEqual(pressed["chunkSize"], self.mod.OPT_CHUNK_PRESSURE_SIZE)
        self.assertEqual(pressed["maxChunks"], self.mod.OPT_CHUNK_PRESSURE_MAX_CHUNKS)
        self.mod.noteOptChunkRound(1, holdSec=30)
        self.assertTrue(self.mod.isOptChunkUnderPressure())
        self.mod._optChunkPressureUntil = 0.0
        self.assertFalse(self.mod.isOptChunkUnderPressure())

    def testQmtPathUsedWhenFloorOk(self):
        mod = self.mod
        mapping = {
            "510050.SH": ["%08d.SHO" % (10000000 + i) for i in range(100)],
            "510300.SH": ["%08d.SHO" % (10000100 + i) for i in range(100)],
            "510500.SH": ["%08d.SHO" % (10000200 + i) for i in range(100)],
            "588000.SH": ["%08d.SHO" % (10000300 + i) for i in range(100)],
            "159915.SZ": ["%08d.SZO" % (90000000 + i) for i in range(100)],
        }

        class Client:
            def call(self, method, params, timeout_seconds=None):
                if method != "get_option_undl_data":
                    raise AssertionError(method)
                undl = (params or {}).get("undl_code_ref")
                if not undl:
                    raise AssertionError("empty undl_code_ref")
                return mapping[undl]

        class Xt:
            def __init__(self):
                self.client = Client()

        codes, per = mod.loadEtfOptionCodesFromQmt(Xt())
        self.assertEqual(len(codes), 500)
        self.assertTrue(mod.optionChainMeetsFloor(per))


class OptTickDownloadQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testStartYmdLookback(self):
        from datetime import date as dateCls

        self.assertEqual(
            self.mod.optTickDownloadStartYmd(dateCls(2026, 8, 25), 7),
            "20260818",
        )

    def testSzoFirstAndSkipFresh(self):
        from datetime import date as dateCls

        todayMs = datetime(2026, 8, 25, 15, 0).timestamp() * 1000.0
        oldMs = datetime(2026, 8, 21, 10, 0).timestamp() * 1000.0
        codes = [
            "10012153.SHO",
            "90007784.SZO",
            "10012156.SHO",
            "90007783.SZO",
        ]
        tickMap = {
            "10012153.SHO": {"time": todayMs},
            "90007783.SZO": {"time": oldMs},
        }
        need = self.mod.optionCodesNeedingTickDownload(
            codes, tickMap, dateCls(2026, 8, 25)
        )
        self.assertEqual(
            need,
            ["90007784.SZO", "90007783.SZO", "10012153.SHO", "10012156.SHO"],
        )
        liveMap = {
            "10012153.SHO": {"time": todayMs, "lastPrice": 0.08, "volume": 3},
        }
        self.assertEqual(
            self.mod.optionCodesNeedingTickDownload(
                ["10012153.SHO", "10012156.SHO"], liveMap, dateCls(2026, 8, 25)
            ),
            ["10012156.SHO"],
        )
        self.assertFalse(
            self.mod.optionTickLooksUsable({"time": todayMs}, dateCls(2026, 8, 25))
        )
        self.assertTrue(
            self.mod.optionTickLooksUsable(
                {"time": todayMs, "lastPrice": 0.08}, dateCls(2026, 8, 25)
            )
        )
        self.assertFalse(
            self.mod.optionTickShouldReplace(
                {"time": todayMs, "lastPrice": 0.08},
                {"lastPrice": 0.0, "bidPrice": [0, 0]},
                dateCls(2026, 8, 25),
            )
        )

    def testSzoSortedByDailyVolume(self):
        from datetime import date as dateCls

        codes = ["90007784.SZO", "90007783.SZO", "10012153.SHO"]
        dailyMap = {
            "90007784.SZO": {"volume": 100.0},
            "90007783.SZO": {"volume": 500.0},
            "10012153.SHO": {"volume": 999.0},
        }
        need = self.mod.optionCodesNeedingTickDownload(
            codes, {}, dateCls(2026, 8, 25), dailyMap
        )
        self.assertEqual(
            need,
            ["90007783.SZO", "90007784.SZO", "10012153.SHO"],
        )


class OptionChainRepairTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testZipGrewVsSeed(self):
        self.assertTrue(self.mod.optionChainZipGrew(632, 542))
        self.assertFalse(self.mod.optionChainZipGrew(542, 542))
        self.assertFalse(self.mod.optionChainZipGrew(500, 542))

    def testSameSizeSettlesAfterOpenConfirm(self):
        early = datetime(2026, 8, 27, 9, 20, 0)
        late = datetime(2026, 8, 27, 9, 35, 0)
        self.assertFalse(self.mod.optionChainZipSameSizeSettled(early, 5))
        self.assertFalse(self.mod.optionChainZipSameSizeSettled(late, 1))
        self.assertTrue(self.mod.optionChainZipSameSizeSettled(late, 2))

    def testDecideZipState(self):
        now = datetime(2026, 8, 27, 9, 40, 0)
        self.assertEqual(
            self.mod.decideOptionChainZipState(True, 0, now),
            "complete",
        )
        self.assertEqual(
            self.mod.decideOptionChainZipState(False, 1, now),
            "retry",
        )
        self.assertEqual(
            self.mod.decideOptionChainZipState(False, 2, now),
            "complete",
        )
        self.assertEqual(
            self.mod.decideOptionChainZipState(
                False, 2, datetime(2026, 8, 27, 9, 20, 0)
            ),
            "retry",
        )

    def testConfirmZipUntilLateMorning(self):
        now = datetime(2026, 8, 27, 10, 0, 0)
        self.assertTrue(
            self.mod.shouldConfirmOptionChainZip(now, True, 0.0, 700.0, 600.0)
        )
        self.assertFalse(
            self.mod.shouldConfirmOptionChainZip(now, True, 0.0, 100.0, 600.0)
        )
        self.assertFalse(
            self.mod.shouldConfirmOptionChainZip(
                datetime(2026, 8, 27, 12, 0, 0), True, 0.0, 700.0, 600.0
            )
        )
        self.assertFalse(
            self.mod.shouldConfirmOptionChainZip(now, False, 0.0, 700.0, 600.0)
        )

    def testCompleteZipStillConfirmsBefore1130(self):
        asof = date(2026, 8, 27)
        now = datetime(2026, 8, 27, 10, 15, 0)
        self.assertTrue(
            self.mod.shouldAttemptOptionChainZip(
                asof, asof, 700.0, 8.0, 0.0,
                chainComplete=True, lastZipMono=0.0, now=now, confirmSec=600.0,
            )
        )
        self.assertFalse(
            self.mod.shouldAttemptOptionChainZip(
                asof, asof, 700.0, 8.0, 0.0,
                chainComplete=True, lastZipMono=0.0,
                now=datetime(2026, 8, 27, 12, 0, 0), confirmSec=600.0,
            )
        )

    def testInterleaveShoSzo(self):
        self.assertEqual(
            self.mod.interleaveTwo(["a.SHO", "b.SHO"], ["x.SZO"]),
            ["a.SHO", "x.SZO", "b.SHO"],
        )
        self.assertEqual(self.mod.interleaveTwo([], ["x.SZO"]), ["x.SZO"])

    def testMetaNeedListedFirstAndRetry(self):
        chain = ["old.SHO", "new.SHO", "bad.SHO", "szo.SZO"]
        have = {"old.SHO"}
        listed = ["new.SHO", "szo.SZO"]
        triedAt = {"bad.SHO": 10.0}
        out = self.mod.optionMetaNeedCodes(
            chain, have, listed, triedAt, nowMono=20.0, retrySec=60.0,
        )
        self.assertEqual(out, ["new.SHO", "szo.SZO"])
        out2 = self.mod.optionMetaNeedCodes(
            chain, have, listed, triedAt, nowMono=80.0, retrySec=60.0,
        )
        self.assertEqual(out2[0], "new.SHO")
        self.assertIn("bad.SHO", out2)
        self.assertEqual(out2[1], "szo.SZO")
        prefer = self.mod.optionMetaPreferCodes(
            ["90008029.SZO", "old.SHO", ""],
            ["new.SHO", "90008029.SZO"],
            have={"old.SHO"},
        )
        self.assertEqual(prefer, ["90008029.SZO", "new.SHO"])

    def testPollListedBoostsFront(self):
        codes = ["thin.SHO", "hot.SHO", "new.SHO"]
        ticks = {"hot.SHO": {"vol": 9000}}
        out = self.mod.orderOptionCodesForPoll(
            codes, ticks, {}, listedSet={"new.SHO"}
        )
        self.assertEqual(out[0], "new.SHO")
        self.assertEqual(out[1], "hot.SHO")

    def testListedLookbackUsesTradeDaysAcrossWeekend(self):
        monday = date(2026, 8, 31)
        tradeDates = [
            date(2026, 8, 24),
            date(2026, 8, 25),
            date(2026, 8, 26),
            date(2026, 8, 27),
            date(2026, 8, 28),
            date(2026, 8, 31),
        ]
        start = self.mod.optionListedLookbackStart(
            monday, tradeDates, tradeDays=4, calendarFallbackDays=10,
        )
        self.assertEqual(start, date(2026, 8, 26))
        sql, params = self.mod.optBasicListedOnQuery(monday, startDate=start)
        self.assertEqual(params[0], date(2026, 8, 26))
        self.assertEqual(params[1], monday)
        self.assertLessEqual(start, date(2026, 8, 27))

    def testListedLookbackFallsBackToCalendar(self):
        self.assertEqual(
            self.mod.optionListedLookbackStart(
                date(2026, 8, 27), [], tradeDays=4, calendarFallbackDays=10,
            ),
            date(2026, 8, 17),
        )

    def testLiveWaitIgnoresTickGap(self):
        waitLive = self.mod.optionLivePollWaitSec(
            True, 100.0, 100.0, liveSec=0.2, stillNeedN=90, dlN=0, restSec=0.2,
        )
        self.assertAlmostEqual(waitLive, 0.2, places=3)
        waitRest = self.mod.optionLivePollWaitSec(
            True, 100.0, 0.0, liveSec=0.2, restSec=1.5,
        )
        self.assertAlmostEqual(waitRest, 1.5, places=3)
        waitNoDailyGate = self.mod.optionLivePollWaitSec(
            True, 100.0, 98.0, liveSec=10.0, restSec=0.4,
        )
        self.assertAlmostEqual(waitNoDailyGate, 0.4, places=3)
        waitIdleGap = self.mod.optionLivePollWaitSec(
            False, 100.0, 0.0, idleSec=30.0,
            downloadTicks=True, stillNeedN=10, dlN=0,
        )
        self.assertAlmostEqual(waitIdleGap, 2.0, places=3)
        waitIdle = self.mod.optionLivePollWaitSec(
            False, 100.0, 0.0, idleSec=30.0,
            downloadTicks=False, stillNeedN=10, dlN=0,
        )
        self.assertAlmostEqual(waitIdle, 30.0, places=3)
        waitCatchup = self.mod.optionLivePollWaitSec(
            False, 100.0, 0.0, idleSec=30.0, restSec=0.4, catchup=True,
        )
        self.assertAlmostEqual(waitCatchup, 0.4, places=3)
        waitFail = self.mod.optionLivePollWaitSec(
            False, 100.0, 0.0, idleSec=30.0, restSec=0.4, catchup=True,
            failChunks=8,
        )
        self.assertAlmostEqual(waitFail, self.mod.OPT_FAIL_BACKOFF_SEC, places=3)
        waitLiveFail = self.mod.optionLivePollWaitSec(
            True, 100.0, 100.0, restSec=0.4, failChunks=2,
        )
        self.assertAlmostEqual(waitLiveFail, self.mod.OPT_FAIL_BACKOFF_SEC, places=3)


class OptWarnCooldownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testNamedLogCooldown(self):
        store = {}
        self.assertTrue(
            self.mod.shouldEmitNamedLog(store, "OPT_FAIL", 100.0, 60.0)
        )
        self.assertFalse(
            self.mod.shouldEmitNamedLog(store, "OPT_FAIL", 150.0, 60.0)
        )
        self.assertTrue(
            self.mod.shouldEmitNamedLog(store, "OPT_FAIL", 160.0, 60.0)
        )
        self.assertTrue(
            self.mod.shouldEmitNamedLog(store, "SUBSCRIBE_NOOP", 150.0, 60.0)
        )
        self.assertEqual(self.mod.OPT_WARN_COOLDOWN_SEC, 60.0)
        self.assertEqual(self.mod.OPT_FAIL_BACKOFF_SEC, 30.0)
        self.assertEqual(self.mod.SUBSCRIBE_NOOP_BACKOFF_SEC, 30.0)


if __name__ == "__main__":
    unittest.main()

# 代码更新时间：2026-08-25 18:18
# 代码更新时间：2026-08-25 18:27 占位日K 不填现货；rt_k 行转换
# 代码更新时间：2026-08-25 18:47 盘中 tick 优先日K；禁盘中灌库
# 代码更新时间：2026-08-25 19:11 有量合约优先轮询
# 代码更新时间：2026-08-25 19:18 期权链日切/周期重拉
# 代码更新时间：2026-08-25 19:23 期权链仅日切重拉
# 代码更新时间：2026-08-25 19:34 compactOptionMeta 实盘字段
# 代码更新时间：2026-08-25 20:41 调整合约垫后；ping 12s；快照 JSON TTL
# 代码更新时间：2026-08-26 08:45 单实例抢锁不误杀桥接/Flask
# 代码更新时间：2026-08-26 08:46 不杀自身/父进程/非 python
# 代码更新时间：2026-08-26 09:10 启动等 RPC、订全推失败重试
# 代码更新时间：2026-08-26 09:23 盘中停 META；RPC 串行；optMeta 预填；片失败对半重试
# 代码更新时间：2026-08-26 09:47 盘中灌当日 tick；按标的整链轮询量仓
# 代码更新时间：2026-08-26 09:53 盘中量仓改 15s 日K；tick 灌库仅盘前/午休
# 代码更新时间：2026-08-26 09:58 盘中跳过废日 tick，先刷 1d 量仓
# 代码更新时间：2026-08-26 10:08 588000 日K 热刷；其余链 12s
# 代码更新时间：2026-08-26 10:15 五条期权链同等刷新 1d
# 代码更新时间：2026-08-26 10:28 剔除 1d 占位柱；SZO 补灌当日日K
# 代码更新时间：2026-08-26 12:09 午休并入直播窗，tick 仅盘前/15:31后灌
# 代码更新时间：2026-08-26 13:37 创业板 1d 定期灌库，解开上午冻结柱
# 代码更新时间：2026-08-26 14:50 沪市片 200；SZO 1d 订阅每日确认
def _packDatRecord(ts, o, h, low, c, vol, amount, oi, preClose, scale=1000):
    """按实测布局拼一条 64 字节 .DAT 记录（小端 16×u32）。"""
    u = [0] * 16
    u[2] = int(ts)
    u[3] = int(round(o * scale))
    u[4] = int(round(h * scale))
    u[5] = int(round(low * scale))
    u[6] = int(round(c * scale))
    u[8] = int(vol)
    u[10] = int(amount) & 0xFFFFFFFF
    u[11] = (int(amount) >> 32) & 0xFFFFFFFF
    u[12] = int(oi)
    u[15] = int(round(preClose * scale))
    return struct.Struct("<16I").pack(*u)


class QmtLocalDatReadTest(unittest.TestCase):
    """本地 datadir 直读：格式、尾部残片、尾部截取、与下游合成的衔接。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testDatPathBuild(self):
        p = self.mod.qmtDatPath("90007420.SZO", "1m")
        self.assertTrue(p.replace("\\", "/").endswith("SZO/60/90007420.DAT"), p)
        p = self.mod.qmtDatPath("10011516.SHO", "1d")
        self.assertTrue(p.replace("\\", "/").endswith("SHO/86400/10011516.DAT"), p)
        # 周期未知、缺市场后缀都不该拼出路径
        self.assertIsNone(self.mod.qmtDatPath("90007420.SZO", "tick"))
        self.assertIsNone(self.mod.qmtDatPath("90007420", "1m"))
        self.assertIsNone(self.mod.qmtDatPath("", "1m"))

    def testParseRecordFields(self):
        ts = 1787900400  # 2026-08-28 15:00:00 +08
        blob = _packDatRecord(ts, 2.5, 2.6, 2.4, 2.568, 1234, 5_000_000_000, 48946, 2.15)
        recs = self.mod.parseQmtDatBytes(blob)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        # time 输出毫秒，与 RPC 对齐
        self.assertEqual(r["time"], ts * 1000)
        self.assertAlmostEqual(r["close"], 2.568, places=6)
        self.assertAlmostEqual(r["open"], 2.5, places=6)
        self.assertAlmostEqual(r["high"], 2.6, places=6)
        self.assertAlmostEqual(r["low"], 2.4, places=6)
        self.assertEqual(r["volume"], 1234)
        # amount 是跨两个 u32 的 u64，必须能过 4G
        self.assertAlmostEqual(r["amount"], 5_000_000_000.0, places=1)
        self.assertEqual(r["openInterest"], 48946)
        self.assertAlmostEqual(r["preClose"], 2.15, places=6)
        self.assertEqual(r["suspendFlag"], 0)

    def testTrailingFragmentDropped(self):
        """15432 = 241×64 + 8：尾部 8 字节残片必须丢弃，不能当成一条记录。"""
        blob = _packDatRecord(1787900400, 1, 1, 1, 1, 1, 1, 0, 1) * 2
        self.assertEqual(len(self.mod.parseQmtDatBytes(blob + b"\x00" * 8)), 2)
        # 只有残片的空文件解析为 0 条
        self.assertEqual(self.mod.parseQmtDatBytes(b"\x00" * 8), [])
        self.assertEqual(self.mod.parseQmtDatBytes(b""), [])

    def testReadFileTailOnly(self):
        import tempfile

        base = 1787900400
        blob = b"".join(
            _packDatRecord(base + i * 60, 1, 1, 1, 1 + i, 10, 100, 0, 1)
            for i in range(10)
        )
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.DAT")
            with open(path, "wb") as fh:
                fh.write(blob + b"\x00" * 8)
            recs = self.mod.readQmtDatFile(path, 3)
            self.assertEqual(len(recs), 3)
            # 取的是最后 3 条，不是最前 3 条
            self.assertEqual(recs[-1]["time"], (base + 9 * 60) * 1000)
            self.assertEqual(recs[0]["time"], (base + 7 * 60) * 1000)
            self.assertEqual(len(self.mod.readQmtDatFile(path, 0)), 10)
            self.assertEqual(self.mod.readQmtDatFile(os.path.join(d, "none.DAT"), 3), [])

    def testLocalBarsFeedMinuteAndDaily(self):
        """本地记录必须能被既有下游直接消费，否则接入无意义。"""
        today = date.today()
        stamp = datetime(today.year, today.month, today.day, 14, 30).timestamp()
        recs = [
            self.mod.parseQmtDatBytes(
                _packDatRecord(stamp, 2.5, 2.6, 2.4, 2.5, 300, 1000, 48946, 2.15)
            )[0],
            self.mod.parseQmtDatBytes(
                _packDatRecord(stamp + 60, 2.5, 2.6, 2.4, 2.7, 200, 1000, 48950, 2.15)
            )[0],
        ]
        tick = self.mod.szoMinuteRecordsToTick(recs, today)
        self.assertIsNotNone(tick)
        self.assertAlmostEqual(tick["lastPrice"], 2.7, places=6)
        self.assertAlmostEqual(tick["volume"], 500.0, places=6)  # 当日分钟量累加
        self.assertAlmostEqual(tick["openInt"], 48950.0, places=6)

        bar = self.mod.optionDailyBarFromRecords(recs, asof=today)
        self.assertIsNotNone(bar)
        self.assertEqual(bar["d"], today)
        self.assertAlmostEqual(bar["preClose"], 2.15, places=6)
        self.assertAlmostEqual(bar["oi"], 48950.0, places=6)

    def testLocalReadDisabledReturnsEmpty(self):
        """开关关掉时必须整体回退 RPC，不做半吊子。"""
        saved = self.mod.QMT_DAT_ENABLED
        try:
            self.mod.QMT_DAT_ENABLED = False
            self.assertEqual(self.mod.readQmtLocalBars(["90007420.SZO"], "1m", 300), {})
        finally:
            self.mod.QMT_DAT_ENABLED = saved

    def testOptionDatUsesFourDecimalScale(self):
        """SHO/SZO 权利金 4 位小数；按股票 /1000 会把虚值认沽 0.039 读成 0.39。"""
        self.assertEqual(self.mod.qmtDatPriceScale("90007080.SZO"), 10000.0)
        self.assertEqual(self.mod.qmtDatPriceScale("10011516.SHO"), 10000.0)
        self.assertEqual(self.mod.qmtDatPriceScale("159915.SZ"), 1000.0)
        ts = 1788313920
        blob = _packDatRecord(
            ts, 0.0393, 0.0394, 0.0391, 0.0390, 110, 1000, 47348, 0.0394,
            scale=10000,
        )
        recs = self.mod.parseQmtDatBytes(
            blob, priceScale=self.mod.qmtDatPriceScale("90007080.SZO"),
        )
        self.assertAlmostEqual(recs[0]["close"], 0.0390, places=6)
        self.assertAlmostEqual(recs[0]["open"], 0.0393, places=6)
        stockScale = self.mod.parseQmtDatBytes(blob)
        self.assertAlmostEqual(stockScale[0]["close"], 0.390, places=6)
        meta = {"k": 3.1, "cp": "P", "undl": "159915.SZ"}
        spot = 3.35
        self.assertFalse(
            self.mod.optionPremiumLooksImplausible(0.0390, meta, spot)
        )
        self.assertFalse(
            self.mod.optionPremiumLooksImplausible(0.390, meta, spot)
        )
        asof = date(2026, 9, 2)
        tick = self.mod.szoMinuteRecordsToTick(recs, asof)
        self.assertIsNotNone(tick)
        self.assertTrue(
            self.mod.shouldApplySzoMinuteTick(None, tick, asof, meta, spot)
        )
        self.assertAlmostEqual(tick["lastPrice"], 0.0390, places=6)


class OptionLiveRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testFingerprintIgnoresIdentityReplay(self):
        tick = {
            "lastPrice": 0.12,
            "bidPrice": [0.11, 0.10],
            "askPrice": [0.13, 0.14],
            "volume": 8,
            "time": 1788229569000,
        }
        self.assertFalse(self.mod.optionTickPayloadChanged(tick, dict(tick)))
        newer = dict(tick)
        newer["lastPrice"] = 0.13
        self.assertTrue(self.mod.optionTickPayloadChanged(tick, newer))

    def testFingerprintDetectsDeepBookAndLevelVolumeChanges(self):
        tick = {
            "lastPrice": 0.12,
            "bid1": 0.11,
            "ask1": 0.13,
            "bidPrice": [0.11, 0.10, 0.09, 0.08, 0.07],
            "askPrice": [0.13, 0.14, 0.15, 0.16, 0.17],
            "bidVol": [1, 2, 3, 4, 5],
            "askVol": [5, 4, 3, 2, 1],
            "volume": 8,
            "openInt": 99,
            "time": 1788229569000,
        }
        deepPrice = dict(tick)
        deepPrice["bidPrice"] = [0.11, 0.101, 0.09, 0.08, 0.07]
        self.assertTrue(self.mod.optionTickPayloadChanged(tick, deepPrice))
        levelVolume = dict(tick)
        levelVolume["askVol"] = [5, 40, 3, 2, 1]
        self.assertTrue(self.mod.optionTickPayloadChanged(tick, levelVolume))

    def testStaleUsableNeedsRefresh(self):
        from datetime import date as dateCls

        now = datetime(2026, 9, 1, 10, 40, 0)
        staleMs = datetime(2026, 9, 1, 10, 26, 0).timestamp() * 1000.0
        freshMs = datetime(2026, 9, 1, 10, 39, 30).timestamp() * 1000.0
        asof = dateCls(2026, 9, 1)
        codes = ["10010971.SHO", "10010972.SHO", "90007424.SZO"]
        ticks = {
            "10010971.SHO": {
                "time": staleMs,
                "lastPrice": 0.08,
                "bidPrice": [0.07],
                "askPrice": [0.09],
            },
            "10010972.SHO": {
                "time": freshMs,
                "lastPrice": 0.09,
                "bidPrice": [0.08],
                "askPrice": [0.10],
            },
        }
        need = self.mod.optionCodesNeedingLiveTickRefresh(
            codes, ticks, asof, now=now, maxAgeSec=60.0,
        )
        self.assertEqual(need, ["10010971.SHO"])

    def testLivePollSkipsUsableBook(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 9, 1)
        codes = ["10010971.SHO", "10010972.SHO"]
        ticks = {
            "10010971.SHO": {
                "lastPrice": 0.08,
                "bidPrice": [0.07],
                "askPrice": [0.09],
                "time": 1788229569000,
            },
        }
        poll = self.mod.optionShoLivePollCodes(codes, ticks, {}, asof, 30.0)
        self.assertEqual(poll, ["10010972.SHO"])

    def testRefreshCooldown(self):
        self.assertTrue(self.mod.optionTickRefreshReady("a", 40.0, {}, 30.0))
        self.assertFalse(
            self.mod.optionTickRefreshReady("a", 40.0, {"a": 20.0}, 30.0)
        )
        self.assertTrue(
            self.mod.optionTickRefreshReady("a", 51.0, {"a": 20.0}, 30.0)
        )

    def testRefreshQueueRotates(self):
        codes = [f"c{i:03d}.SHO" for i in range(6)]
        self.assertEqual(
            self.mod.optionLiveRefreshQueue(codes, 0, 2)[:2],
            ["c000.SHO", "c001.SHO"],
        )
        self.assertEqual(
            self.mod.optionLiveRefreshQueue(codes, 1, 2)[:2],
            ["c002.SHO", "c003.SHO"],
        )

    def testRefreshChunkAdaptive(self):
        self.assertEqual(self.mod.optionLiveRefreshChunkSize(7185, 0.1), 40)
        self.assertEqual(self.mod.optionLiveRefreshChunkSize(7000, 0.99), 40)
        self.assertEqual(self.mod.optionLiveRefreshChunkSize(7185, 1.0), 20)
        self.assertEqual(self.mod.optionLiveRefreshChunkSize(6999, 0.0), 20)
        self.assertEqual(self.mod.OPT_LIVE_REFRESH_CHUNK_FAST, 40)

    def testRefreshPriorityN(self):
        self.assertEqual(self.mod.optionLiveRefreshPriorityN(40), 30)
        self.assertEqual(self.mod.optionLiveRefreshPriorityN(20), 15)

    def testRefreshPriorityChunkVolumeFirst(self):
        stale = [
            "a.SHO", "b.SHO", "c.SHO", "d.SHO",
            "e.SHO", "f.SHO", "g.SHO", "h.SHO",
        ]
        meta = {
            "a.SHO": {"undl": "510050.SH"},
            "b.SHO": {"undl": "510050.SH"},
            "c.SHO": {"undl": "510300.SH"},
            "d.SHO": {"undl": "510300.SH"},
            "e.SHO": {"undl": "510500.SH"},
            "f.SHO": {"undl": "510500.SH"},
            "g.SHO": {"undl": "588000.SH"},
            "h.SHO": {"undl": "588000.SH"},
        }
        ticks = {
            "a.SHO": {"vol": 10},
            "b.SHO": {"vol": 1},
            "c.SHO": {"vol": 20},
            "d.SHO": {"vol": 1},
            "e.SHO": {"vol": 30},
            "f.SHO": {"vol": 1},
            "g.SHO": {"vol": 40},
            "h.SHO": {"vol": 1},
        }
        out = self.mod.optionLiveRefreshPriorityChunk(
            stale, 0, 5, ticks, meta, priorityN=4,
        )
        self.assertEqual(len(out), 5)
        self.assertEqual(set(out[:4]), {"a.SHO", "c.SHO", "e.SHO", "g.SHO"})
        self.assertIn(out[4], {"b.SHO", "d.SHO", "f.SHO", "h.SHO"})

    def testRefreshPriorityTailRotates(self):
        stale = ["p.SHO", "q.SHO", "r.SHO", "s.SHO"]
        meta = {c: {"undl": "510050.SH"} for c in stale}
        ticks = {"p.SHO": {"vol": 100}, "q.SHO": {"vol": 1},
                 "r.SHO": {"vol": 1}, "s.SHO": {"vol": 1}}
        a = self.mod.optionLiveRefreshPriorityChunk(
            stale, 0, 2, ticks, meta, priorityN=1,
        )
        b = self.mod.optionLiveRefreshPriorityChunk(
            stale, 2, 2, ticks, meta, priorityN=1,
        )
        self.assertEqual(a[0], "p.SHO")
        self.assertEqual(b[0], "p.SHO")
        self.assertNotEqual(a[1], b[1])

    def testSzoLiveDownloadDue(self):
        self.assertTrue(self.mod.optionSzoLiveDownloadDue(1.0, 0.0, 30.0))
        self.assertFalse(self.mod.optionSzoLiveDownloadDue(35.0, 10.0, 30.0))
        self.assertTrue(self.mod.optionSzoLiveDownloadDue(40.0, 10.0, 30.0))

    def testSzoLiveTwoPhaseRollbackUsesNaturalMinuteEightAndThirtyEight(self):
        m = self.mod
        self.assertIsNone(m.optionSzoLivePhaseKey(
            datetime(2026, 9, 24, 13, 30, 7), thirdPhaseEnable=False,
        ))
        phase8 = m.optionSzoLivePhaseKey(
            datetime(2026, 9, 24, 13, 30, 8), thirdPhaseEnable=False,
        )
        self.assertEqual(phase8, "202609241330:08")
        self.assertEqual(
            m.optionSzoLivePhaseKey(
                datetime(2026, 9, 24, 13, 30, 37), thirdPhaseEnable=False,
            ),
            phase8,
        )
        phase38 = m.optionSzoLivePhaseKey(
            datetime(2026, 9, 24, 13, 30, 38), thirdPhaseEnable=False,
        )
        self.assertEqual(phase38, "202609241330:38")
        self.assertFalse(m.optionSzoLivePhaseDue(
            datetime(2026, 9, 24, 13, 30, 24), phase8, thirdPhaseEnable=False,
        ))
        self.assertTrue(m.optionSzoLivePhaseDue(
            datetime(2026, 9, 24, 13, 30, 38), phase8, thirdPhaseEnable=False,
        ))
        self.assertTrue(m.optionSzoLivePhaseDue(
            datetime(2026, 9, 24, 13, 31, 8), phase38, thirdPhaseEnable=False,
        ))

    def testSzoLivePhaseWaitReportsNextAllowedPhase(self):
        m = self.mod
        self.assertEqual(
            m.optionSzoLivePhaseWaitSec(
                datetime(2026, 9, 24, 13, 30, 7), thirdPhaseEnable=False,
            ),
            1.0,
        )
        self.assertEqual(
            m.optionSzoLivePhaseWaitSec(
                datetime(2026, 9, 24, 13, 30, 24), thirdPhaseEnable=False,
            ),
            14.0,
        )
        self.assertEqual(
            m.optionSzoLivePhaseWaitSec(
                datetime(2026, 9, 24, 13, 30, 45), thirdPhaseEnable=False,
            ),
            23.0,
        )

    def testSzoLiveThirdPhaseDefaultsOnAfterValidatedAb(self):
        """盘中 A/B 证实后默认启用 :23；显式设 False 仍可退回原双相位。"""
        m = self.mod
        at23 = datetime(2026, 9, 24, 13, 30, 23)
        self.assertTrue(m.OPT_SZO_MIN_THIRD_PHASE_ENABLE)
        self.assertEqual(m.optionSzoLivePhaseSeconds(), (8, 23, 38))
        self.assertEqual(m.optionSzoLivePhaseSeconds(False), (8, 38))
        self.assertEqual(m.optionSzoLivePhaseSeconds(True), (8, 23, 38))
        self.assertEqual(m.optionSzoLivePhaseKey(at23), "202609241330:23")
        self.assertEqual(m.optionSzoLivePhaseKey(at23, thirdPhaseEnable=True), "202609241330:23")
        self.assertEqual(m.optionSzoLivePhaseKey(at23, thirdPhaseEnable=False), "202609241330:08")
        self.assertEqual(
            m.optionSzoLivePhaseWaitSec(at23, thirdPhaseEnable=True),
            15.0,
        )

    def testSzoMinuteOiLeaseUsesBoundedPhaseGrace(self):
        m = self.mod
        nowMs = datetime(2026, 9, 24, 13, 30, 45).timestamp() * 1000.0
        decision = m.szoMinuteOiLeaseDecision(
            {
                "oi": 100,
                "oiTime": nowMs - 50_000,
                "oiSource": "qmt_1m",
            },
            nowMs=nowMs,
            maxAgeSec=45.0,
            activeTtlSec=90.0,
            phaseWaitSec=23.0,
            graceSec=30.0,
        )
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["reason"], "phase_grace")
        self.assertAlmostEqual(decision["ageSec"], 50.0, places=3)

    def testSzoMinuteOiGraceIsDisabledByDefault(self):
        self.assertFalse(self.mod.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE)

    def testSzoMinuteOiLeaseRejectsExpiredWhenPhaseIsNotNear(self):
        m = self.mod
        nowMs = datetime(2026, 9, 24, 13, 30, 20).timestamp() * 1000.0
        decision = m.szoMinuteOiLeaseDecision(
            {
                "oi": 100,
                "oiTime": nowMs - 50_000,
                "oiSource": "qmt_1m",
            },
            nowMs=nowMs,
            maxAgeSec=45.0,
            activeTtlSec=90.0,
            phaseWaitSec=50.0,
            graceSec=30.0,
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason"], "expired")

    def testSzoMinuteOiDiagnosticsSeparatesLeaseReasons(self):
        m = self.mod
        nowMs = datetime(2026, 9, 24, 13, 30, 45).timestamp() * 1000.0
        fresh = "90000101.SZO"
        grace = "90000102.SZO"
        old = "90000103.SZO"
        other = "90000104.SZO"
        meta = {code: {"undl": "159915.SZ"} for code in (fresh, grace, old, other)}
        ticks = {
            fresh: {"oi": 1, "oiTime": nowMs - 20_000, "oiSource": "qmt_1m"},
            grace: {"oi": 1, "oiTime": nowMs - 50_000, "oiSource": "qmt_1m"},
            old: {"oi": 1, "oiTime": nowMs - 100_000, "oiSource": "qmt_1m"},
            other: {"oi": 1, "oiTime": nowMs - 20_000, "oiSource": "tdx"},
        }
        savedGrace = m.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE
        m.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE = True
        try:
            result = m.szoMinuteOiOffloadDiagnostics(
                ticks, meta, nowMs=nowMs, maxAgeSec=45.0, enabled=True
            )
        finally:
            m.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE = savedGrace
        self.assertEqual(result["lease_fresh_n"], 1)
        self.assertEqual(result["lease_grace_n"], 1)
        self.assertEqual(result["lease_expired_n"], 1)
        self.assertEqual(result["eligible_n"], 2)
        self.assertIn(grace, result["eligible"])
        self.assertNotIn(other, result["eligible"])

    def testSzoLivePhaseRollbackDoesNotIncreaseBatchOrCadence(self):
        m = self.mod
        asof = date(2026, 9, 24)
        need = ["90000001.SZO"]
        ticks = {
            need[0]: {
                "lastPrice": 0.08,
                "time": datetime(2026, 9, 24, 13, 30, 0).timestamp() * 1000.0,
            },
        }
        phase8 = m.optionSzoLivePhaseKey(
            datetime(2026, 9, 24, 13, 30, 8), thirdPhaseEnable=False,
        )
        self.assertFalse(m.optionSzoLiveShouldDownload(
            need, ticks, asof, 999.0, 0.0,
            nowWall=datetime(2026, 9, 24, 13, 30, 24),
            lastPhase=phase8, phaseAlign=True, thirdPhaseEnable=False,
        ))
        self.assertTrue(m.optionSzoLiveShouldDownload(
            need, ticks, asof, 1000.0, 0.0,
            nowWall=datetime(2026, 9, 24, 13, 30, 38),
            lastPhase=phase8, phaseAlign=True, thirdPhaseEnable=False,
        ))
        # 冷启动/新挂牌仍保留立即补齐，不被正常 OI 相位调度拖慢。
        self.assertTrue(m.optionSzoLiveShouldDownload(
            need, {}, asof, 1001.0, 0.0,
            nowWall=datetime(2026, 9, 24, 13, 30, 24),
            lastPhase=phase8, phaseAlign=True, thirdPhaseEnable=False,
        ))
        self.assertEqual(m.OPT_LIVE_REFRESH_CHUNK, 20)

    def testSzoLiveShouldDownloadColdIgnoresInterval(self):
        from datetime import date as dateCls

        asof = dateCls(2026, 9, 1)
        need = ["90000001.SZO"]
        self.assertTrue(
            self.mod.optionSzoLiveShouldDownload(
                need, {}, asof, 11.0, 10.0, 30.0,
            )
        )
        ticks = {
            "90000001.SZO": {
                "lastPrice": 0.08,
                "time": 1788229569000,
            },
        }
        self.assertFalse(
            self.mod.optionSzoLiveShouldDownload(
                need, ticks, asof, 11.0, 10.0, 30.0,
            )
        )
        self.assertTrue(
            self.mod.optionSzoLiveShouldDownload(
                need, ticks, asof, 41.0, 10.0, 30.0,
            )
        )

    def testSzoLiveDownloadIncludesOldOiDespiteFreshTdxBook(self):
        """TDX 盘口时钟不得遮蔽独立 OI 时钟的陈旧。"""
        asof = date(2026, 9, 23)
        now = datetime(2026, 9, 23, 14, 30, 0)
        nowMs = now.timestamp() * 1000.0
        codes = ["90000001.SZO", "90000002.SZO", "90000003.SZO"]
        ticks = {
            codes[0]: {
                "fromTdx": True, "lastPrice": 0.08, "time": nowMs - 500,
                "oi": 100, "oiTime": nowMs - 180_000,
            },
            codes[1]: {
                "fromTdx": True, "lastPrice": 0.09, "time": nowMs - 500,
                "oi": 200, "oiTime": nowMs - 90_000,
            },
            codes[2]: {
                "fromTdx": True, "lastPrice": 0.10, "time": nowMs - 500,
                "oi": 300, "oiTime": nowMs - 10_000,
            },
        }

        need = self.mod.optionSzoLiveDownloadCodes(
            codes, ticks, asof, now, {}, 100.0,
        )

        self.assertEqual(need, codes[:2])


class TdxOptOverlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def _tdxTick(self, ageSec=1.0):
        ms = (time.time() - ageSec) * 1000.0
        return {
            "fromTdx": True,
            "last": 0.08,
            "lastPrice": 0.08,
            "bid1": 0.079,
            "ask1": 0.081,
            "bidPrice": [0.079],
            "askPrice": [0.081],
            "vol": 10,
            "t": ms,
            "time": ms,
            "undl": "159915",
        }

    def testTdxCodeAllowed(self):
        """TDX 合并准入：竞价时段 QMT 主导链的 SHO 不被 TDX 覆盖；其余照旧。

        `qmtLedChain` 是**分时段**的（非竞价时段让 TDX 兜底），故显式打桩
        `isWatchdogWindow`，避免测试结果随运行时刻变化。
        """
        from unittest.mock import patch

        m = self.mod
        self.assertTrue(m.tdxOptCodeAllowed("90007074.SZO"))
        # 510050 / 159915：约定走通达信（任何时段都允许 TDX 覆盖）
        self.assertTrue(m.tdxOptCodeAllowed("10010975.SHO", {"undl": "510050"}))
        self.assertFalse(m.tdxOptCodeAllowed("10019999.SHO", {"undl": "159919"}))
        with patch.object(m, "isWatchdogWindow", lambda *a, **k: True):
            # 竞价时段：510300 / 510500 / 588000 由 QMT 主导，TDX 不覆盖
            self.assertFalse(m.tdxOptCodeAllowed("10011001.SHO", {"undl": "510300"}))
            self.assertFalse(m.tdxOptCodeAllowed("10012399.SHO", {"undl": "510500"}))
            self.assertFalse(m.tdxOptCodeAllowed("10011073.SHO", {"undl": "588000"}))
        with patch.object(m, "isWatchdogWindow", lambda *a, **k: False):
            # 非竞价时段（午休/盘后）：QMT 自身也陈旧，必须让 TDX 兜底
            self.assertTrue(m.tdxOptCodeAllowed("10011001.SHO", {"undl": "510300"}))
            self.assertTrue(m.tdxOptCodeAllowed("10012399.SHO", {"undl": "510500"}))
            self.assertTrue(m.tdxOptCodeAllowed("10011073.SHO", {"undl": "588000"}))

    def testQmtLedChainGate(self):
        """QMT 主导链判定：只在连续竞价时段为真，且受开关控制。"""
        from unittest.mock import patch

        m = self.mod
        self.assertTrue(m.QMT_LED_ENABLE)
        with patch.object(m, "isWatchdogWindow", lambda *a, **k: True):
            for six in ("510500", "510300", "588000"):
                self.assertTrue(m.qmtLedChain(six), six)
                self.assertTrue(m.qmtLedChain(f"{six}.SH"), six)
            for six in ("510050", "159915", ""):
                self.assertFalse(m.qmtLedChain(six), six)
        with patch.object(m, "isWatchdogWindow", lambda *a, **k: False):
            # 非竞价时段一律不主导（让 TDX 兜底）
            self.assertFalse(m.qmtLedChain("510500"))
        # SZO 不受 QMT 主导链影响（深市无订阅回调，仍靠 TDX/轮询）
        self.assertTrue(m.tdxOptCodeAllowed("90007074.SZO", {"undl": "159915"}))

    def testFreshTdxBlocksQmtReplace(self):
        asof = date.today()
        existing = self._tdxTick(1.0)
        qmt = {"last": 0.07, "lastPrice": 0.07, "vol": 8, "t": existing["t"]}
        self.assertFalse(self.mod.optionTickShouldReplace(existing, qmt, asof))
        incoming = dict(existing)
        incoming["last"] = 0.082
        incoming["lastPrice"] = 0.082
        self.assertTrue(self.mod.optionTickShouldReplace(existing, incoming, asof))

    def testStaleTdxAllowsQmtFallback(self):
        """过期 fromTdx 也不整票替换；量仓走 FillQty（保留买卖档）。"""
        asof = date.today()
        existing = self._tdxTick(30.0)
        qmt = {
            "last": 0.07,
            "lastPrice": 0.07,
            "vol": 8,
            "oi": 1234,
            "openInt": 1234,
            "bid1": 0.069,
            "ask1": 0.071,
            "t": time.time() * 1000.0,
        }
        self.assertFalse(self.mod.optionTickShouldReplace(existing, qmt, asof))
        patched = self.mod.optionTickFillQtyFromIncoming(existing, qmt)
        self.assertIsNotNone(patched)
        self.assertEqual(patched.get("oi"), 1234)
        self.assertEqual(patched.get("last"), 0.07)
        self.assertEqual(patched.get("bid1"), existing.get("bid1"))
        self.assertTrue(patched.get("qtyFromQmt"))

    def testSzoMinuteSkippedWhenTdxFresh(self):
        asof = date.today()
        existing = self._tdxTick(1.0)
        minute = {"last": 0.09, "lastPrice": 0.09, "time": existing["t"]}
        self.assertFalse(self.mod.shouldApplySzoMinuteTick(existing, minute, asof))

    def testSzoMinuteOverlayPreservesTdxBookAndObservationClock(self):
        """分钟线不含盘口，补齐时不能抹掉 TDX 的 book/bookTime。"""
        sourceMs = (time.time() - 180.0) * 1000.0
        existing = {
            "fromTdx": True,
            "last": 0.08, "lastPrice": 0.08, "vol": 10, "oi": 100,
            "bid1": 0.079, "ask1": 0.081,
            "bidPrice": [0.079], "askPrice": [0.081],
            "bookSource": "tdx", "lastSource": "tdx",
            "time": sourceMs, "t": sourceMs,
            "bookTime": sourceMs,
        }
        minuteMs = time.time() * 1000.0
        minute = {
            "lastPrice": 0.082, "volume": 12, "openInt": 101,
            "openInterest": 101, "time": minuteMs,
        }
        out = self.mod.overlaySzoMinuteTick(existing, minute)
        self.assertIsNotNone(out)
        self.assertTrue(out.get("fromTdx"))
        self.assertEqual(out.get("bid1"), 0.079)
        self.assertEqual(out.get("ask1"), 0.081)
        self.assertEqual(out.get("bookSource"), "tdx")
        self.assertEqual(out.get("bookTime"), sourceMs)
        self.assertEqual(out.get("last"), 0.082)
        self.assertEqual(out.get("vol"), 12)
        self.assertEqual(out.get("oi"), 101)

    def testSzoMinuteOiShadowReportsCandidateWithoutMutation(self):
        code = "90007976.SZO"
        existing = {
            code: {
                "oi": 100,
                "openInt": 100,
                "oiTime": 1_790_040_000_000.0,
                "oiSource": "qmt",
                "bidPrice": [0.10, 0.09],
                "bookTime": 1_790_040_100_000.0,
            }
        }
        minute = {
            code: {
                "openInt": 120,
                "openInterest": 120,
                "time": 1_790_040_060_000.0,
            }
        }
        before = {code: dict(existing[code])}
        stats = self.mod.szoMinuteOiShadowStats(existing, minute)
        self.assertEqual(stats["observed"], 1)
        self.assertEqual(stats["newer"], 1)
        self.assertEqual(stats["mismatch"], 1)
        self.assertEqual(stats["candidate"], 1)
        self.assertEqual(existing, before)
        self.assertEqual(existing[code]["oi"], 100)
        self.assertEqual(existing[code]["oiTime"], 1_790_040_000_000.0)

    def testSzoMinuteOiCandidateUpdatesOnlyNewerOi(self):
        existing = {
            "fromTdx": True,
            "last": 0.08,
            "lastPrice": 0.08,
            "vol": 10,
            "oi": 100,
            "openInt": 100,
            "bid1": 0.079,
            "ask1": 0.081,
            "bidPrice": [0.079, 0.078],
            "askPrice": [0.081, 0.082],
            "time": 1_790_040_100_000.0,
            "t": 1_790_040_100_000.0,
            "bookTime": 1_790_040_100_000.0,
            "oiTime": 1_790_040_000_000.0,
            "oiChangedTime": 1_790_039_900_000.0,
            "oiSource": "qmt",
        }
        minute = {
            "lastPrice": 0.09,
            "volume": 99,
            "openInt": 101,
            "openInterest": 101,
            "time": 1_790_040_180_000.0,
            "oiTime": 1_790_040_060_000.0,
        }
        out = self.mod.overlaySzoMinuteOiCandidate(existing, minute)
        self.assertIsNotNone(out)
        self.assertEqual(out["oi"], 101)
        self.assertEqual(out["openInt"], 101)
        self.assertEqual(out["openInterest"], 101)
        self.assertEqual(out["oiTime"], 1_790_040_060_000.0)
        self.assertEqual(out["oiChangedTime"], 1_790_040_060_000.0)
        self.assertEqual(out["oiSource"], "qmt_1m")
        for key in ("last", "lastPrice", "vol", "bid1", "ask1", "time", "t", "bookTime"):
            self.assertEqual(out[key], existing[key])

        same = dict(minute, openInt=100, openInterest=100)
        sameOut = self.mod.overlaySzoMinuteOiCandidate(existing, same)
        self.assertEqual(sameOut["oiTime"], 1_790_040_060_000.0)
        self.assertEqual(sameOut["oiChangedTime"], existing["oiChangedTime"])
        older = dict(minute, oiTime=1_790_039_000_000.0)
        self.assertIsNone(self.mod.overlaySzoMinuteOiCandidate(existing, older))
        missingClock = dict(minute)
        missingClock.pop("oiTime")
        self.assertIsNone(
            self.mod.overlaySzoMinuteOiCandidate(existing, missingClock)
        )

    def testSzoMinuteOiCandidateCreatesNewContractWhenExistingIsNone(self):
        """新挂牌/首次入缓存合约也必须能由严格更新的分钟 OI 建立记录。"""
        minute = {
            "openInt": 321,
            "openInterest": 321,
            "oiTime": 1_790_040_060_000.0,
            "time": 1_790_040_060_000.0,
        }

        out = self.mod.overlaySzoMinuteOiCandidate(None, minute)

        self.assertIsNotNone(out)
        self.assertEqual(out["oi"], 321)
        self.assertEqual(out["openInt"], 321)
        self.assertEqual(out["openInterest"], 321)
        self.assertEqual(out["oiTime"], 1_790_040_060_000.0)
        self.assertEqual(out["oiChangedTime"], 1_790_040_060_000.0)
        self.assertEqual(out["oiSource"], "qmt_1m")

    def testSzoMinuteOiOffloadDiagnosticsSplitEveryEligibilityGate(self):
        """SZO 卸载诊断必须区分来源、OI、时间和年龄门控。"""
        nowMs = 1_790_040_180_000.0
        meta = {
            "90000001.SZO": {"undl": "159915.SZ"},
            "90000002.SZO": {"undl": "159915.SZ"},
            "90000003.SZO": {"undl": "159915.SZ"},
            "90000004.SZO": {"undl": "159915.SZ"},
            "90000005.SZO": {"undl": "159915.SZ"},
            "90000006.SZO": {"undl": "159915.SZ"},
            "90000007.SZO": {"undl": "510500.SH"},
        }
        ticks = {
            # 通过全部门控。
            "90000001.SZO": {
                "oi": 100,
                "oiTime": nowMs - 10_000,
                "oiSource": "qmt_1m",
            },
            # 来源不属于分钟 QMT。
            "90000002.SZO": {
                "oi": 100,
                "oiTime": nowMs - 10_000,
                "oiSource": "qmt",
            },
            # 分钟 OI 已超过严格年龄窗口。
            "90000003.SZO": {
                "oi": 100,
                "oiTime": nowMs - 60_000,
                "oiSource": "qmt_1m",
            },
            # 未来时间必须 fail-closed。
            "90000004.SZO": {
                "oi": 100,
                "oiTime": nowMs + 1_000,
                "oiSource": "qmt_1m",
            },
            # OI 非正值。
            "90000005.SZO": {
                "oi": 0,
                "oiTime": nowMs - 10_000,
                "oiSource": "qmt_1m",
            },
            # 缺少独立 OI 时间。
            "90000006.SZO": {
                "oi": 100,
                "oiSource": "qmt_1m",
            },
            # 非 159915，不应进入目标池。
            "90000007.SZO": {
                "oi": 100,
                "oiTime": nowMs - 10_000,
                "oiSource": "qmt_1m",
            },
        }

        savedGrace = self.mod.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE
        self.mod.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE = False
        try:
            stats = self.mod.szoMinuteOiOffloadDiagnostics(
                ticks,
                meta,
                nowMs=nowMs,
                maxAgeSec=45.0,
            )
        finally:
            self.mod.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE = savedGrace

        self.assertEqual(stats["target_n"], 6)
        self.assertEqual(stats["source_qmt_1m_n"], 5)
        self.assertEqual(stats["source_other_n"], 1)
        self.assertEqual(stats["oi_positive_n"], 5)
        self.assertEqual(stats["oi_nonpositive_n"], 1)
        self.assertEqual(stats["oi_time_valid_n"], 5)
        self.assertEqual(stats["oi_time_missing_n"], 1)
        self.assertEqual(stats["age_future_n"], 1)
        self.assertEqual(stats["age_old_n"], 1)
        self.assertEqual(stats["age_window_n"], 3)
        self.assertEqual(stats["eligible_n"], 1)
        self.assertEqual(stats["eligible"], {"90000001.SZO"})

    def testSzoMinuteOiAppliesEvenWhenFreshTdxBlocksMinutePrice(self):
        code = "90007976.SZO"
        asof = date.today()
        t1 = datetime(asof.year, asof.month, asof.day, 14, 1).timestamp() * 1000.0
        t2 = datetime(asof.year, asof.month, asof.day, 14, 2).timestamp() * 1000.0
        existing = {
            "fromTdx": True,
            "last": 0.08,
            "lastPrice": 0.08,
            "vol": 10,
            "oi": 100,
            "openInt": 100,
            "bid1": 0.079,
            "ask1": 0.081,
            "bidPrice": [0.079],
            "askPrice": [0.081],
            "time": t2,
            "t": t2,
            "bookTime": t2,
            "oiTime": t1,
        }
        savedRead = self.mod.readQmtLocalBars
        savedTicks = dict(self.mod.optionTicks)
        savedMeta = dict(self.mod.optionMeta)
        try:
            self.mod.optionTicks.clear()
            self.mod.optionTicks[code] = dict(existing)
            self.mod.optionMeta[code] = {"undl": "159915", "k": 3.4, "cp": "C"}
            self.mod.readQmtLocalBars = lambda *_args, **_kwargs: {
                code: [
                    {"time": t2, "close": 0.09, "volume": 0, "openInterest": 101}
                ]
            }
            n = self.mod._applySzoMinuteQuotes(
                self.mod.logging.getLogger("test-szo-oi"),
                None,
                [code],
                asof,
                asof.strftime("%Y%m%d"),
                rpcFallback=False,
                observedAt=datetime(asof.year, asof.month, asof.day, 14, 2, 10),
            )
            out = self.mod.optionTicks[code]
            self.assertEqual(n, 1)
            self.assertEqual(out["oi"], 101)
            self.assertEqual(out["oiSource"], "qmt_1m")
            self.assertEqual(out["last"], existing["last"])
            self.assertEqual(out["vol"], existing["vol"])
            self.assertEqual(out["bid1"], existing["bid1"])
            self.assertEqual(out["bookTime"], existing["bookTime"])
        finally:
            self.mod.readQmtLocalBars = savedRead
            self.mod.optionTicks.clear()
            self.mod.optionTicks.update(savedTicks)
            self.mod.optionMeta.clear()
            self.mod.optionMeta.update(savedMeta)

    def testFetchTdxTimeoutDefaultIsTwoSec(self):
        self.assertGreaterEqual(float(self.mod.TDX_OPT_FETCH_TIMEOUT_SEC), 2.0)
        src = inspect.getsource(self.mod.fetchTdxOptSnapshot)
        self.assertIn("TDX_OPT_FETCH_TIMEOUT_SEC", src)

    def testMergePatchesFromTdxWhenBookUnchanged(self):
        """盘口未变时也补 fromTdx，便于快照可观测。"""
        code = "90007074.SZO"
        ms = time.time() * 1000.0
        existing = {
            "last": 0.08,
            "lastPrice": 0.08,
            "bid1": 0.079,
            "ask1": 0.081,
            "vol": 10,
            "time": ms,
        }
        incoming = {
            "last": 0.08,
            "lastPrice": 0.08,
            "bid1": 0.079,
            "ask1": 0.081,
            "vol": 10,
            "time": ms,
            "fromTdx": True,
            "undl": "159915",
        }
        with self.mod._cacheLock:
            self.mod.optionTicks[code] = dict(existing)
        try:
            n = self.mod.mergeTdxOptionTicks({code: incoming})
            self.assertGreaterEqual(n, 1)
            got = self.mod.optionTicks[code]
            self.assertTrue(got.get("fromTdx"))
        finally:
            with self.mod._cacheLock:
                self.mod.optionTicks.pop(code, None)

    def testMergeWritesChangedDeepBookAndChangedClock(self):
        code = "90007075.SZO"
        oldMs = (time.time() - 2.0) * 1000.0
        observedBefore = (time.time() - 1.0) * 1000.0
        existing = {
            "last": 0.08,
            "lastPrice": 0.08,
            "bid1": 0.079,
            "ask1": 0.081,
            "bidPrice": [0.079, 0.078, 0.077, 0.076, 0.075],
            "askPrice": [0.081, 0.082, 0.083, 0.084, 0.085],
            "bidVol": [1, 2, 3, 4, 5],
            "askVol": [5, 4, 3, 2, 1],
            "vol": 10,
            "oi": 100,
            "fromTdx": True,
            "bookSource": "tdx",
            "time": oldMs,
            "t": oldMs,
            "bookTime": observedBefore,
            "bookChangedTime": observedBefore,
        }
        incoming = dict(existing)
        incoming.update({
            "bidPrice": [0.079, 0.0785, 0.077, 0.076, 0.075],
            "askVol": [5, 40, 3, 2, 1],
            "undl": "159915",
        })
        with self.mod._cacheLock:
            self.mod.optionTicks[code] = dict(existing)
        try:
            n = self.mod.mergeTdxOptionTicks({code: incoming})
            self.assertGreaterEqual(n, 1)
            got = self.mod.optionTicks[code]
            self.assertEqual(got.get("bidPrice"), incoming["bidPrice"])
            self.assertEqual(got.get("askVol"), incoming["askVol"])
            self.assertGreater(got.get("bookChangedTime", 0), observedBefore)
            self.assertEqual(got.get("oi"), 100)
        finally:
            with self.mod._cacheLock:
                self.mod.optionTicks.pop(code, None)

    def testMergeAdvancesTimeWhenBookUnchangedAndAlreadyTdx(self):
        """回归（2026-09-14）：**已是 fromTdx** 的记录，盘口未变时也必须推进时间戳。

        缺陷现场（方案 A 部署后实测，`scripts/probe_tdx_drop_real.py`）：
        `10011545.SHO` TDX 侧 323s 新，daqmt 侧 **12848s** 旧，差 3.5 小时。
        复现结果：`optionTickShouldReplace=True`、`optionTickPayloadChanged=False`
        （指纹差异位**只有 time**）→ 整个 tick 被丢弃。
        根因：补时钟分支被 `and not existing.get("fromTdx")` 限制，
        而该记录早已是 fromTdx=True → 分支永不执行 → **时间戳永久冻结**，
        即使 TDX 每 3s 都在送新时间戳。
        后果：T 板把这些合约一直标成陈旧（灰行），而数据其实一直有更新。
        """
        code = "90007099.SZO"
        oldMs = (time.time() - 12000) * 1000.0      # 3.3 小时前
        newMs = time.time() * 1000.0                # 刚刚
        existing = {
            "last": 0.08, "lastPrice": 0.08,
            "bid1": 0.079, "ask1": 0.081,
            "vol": 402, "oi": 1234,
            "fromTdx": True,                        # ← 关键：已是 fromTdx
            "time": oldMs, "t": oldMs,
        }
        incoming = {
            "last": 0.08, "lastPrice": 0.08,        # 盘口值完全相同
            "bid1": 0.079, "ask1": 0.081,
            "vol": 402, "oi": 1234,
            "fromTdx": True,
            "time": newMs, "t": newMs,              # ← 只有时间戳前进
            "undl": "159915",
        }
        with self.mod._cacheLock:
            self.mod.optionTicks[code] = dict(existing)
        try:
            n = self.mod.mergeTdxOptionTicks({code: incoming})
            self.assertGreaterEqual(n, 1, "盘口未变但时钟前进，仍应算一次有效更新")
            got = self.mod.optionTicks[code]
            self.assertAlmostEqual(
                float(got.get("time") or 0), newMs, delta=1.0,
                msg="common time 必须推进到 TDX 的新时间戳（否则永久判陈旧）")
            # 盘口值不得被改动
            self.assertEqual(got.get("bid1"), 0.079)
            self.assertEqual(got.get("ask1"), 0.081)
            self.assertEqual(got.get("last"), 0.08)
        finally:
            with self.mod._cacheLock:
                self.mod.optionTicks.pop(code, None)

    def testMergeRestampsTdxObservationWhenSourceEventTimeIsUnchanged(self):
        """TDX 缓存重读不变时，观测时钟仍须推进，变动时钟不得伪造更新。"""
        code = "90007101.SZO"
        sourceMs = (time.time() - 120.0) * 1000.0
        oldObservedMs = sourceMs
        existing = {
            "last": 0.08, "lastPrice": 0.08,
            "bid1": 0.079, "ask1": 0.081, "vol": 402,
            "fromTdx": True, "lastSource": "tdx", "bookSource": "tdx",
            "time": sourceMs, "t": sourceMs,
            "lastTime": oldObservedMs, "bookTime": oldObservedMs,
            "lastChangedTime": oldObservedMs, "bookChangedTime": oldObservedMs,
        }
        incoming = {
            "last": 0.08, "lastPrice": 0.08,
            "bid1": 0.079, "ask1": 0.081, "vol": 402,
            # TDX 缓存内容未变时，源端事件时间可以保持不动。
            "time": sourceMs, "t": sourceMs, "undl": "159915",
        }
        with self.mod._cacheLock:
            self.mod.optionTicks[code] = dict(existing)
        try:
            n = self.mod.mergeTdxOptionTicks({code: incoming})
            self.assertGreaterEqual(n, 1)
            got = self.mod.optionTicks[code]
            self.assertAlmostEqual(got["time"], sourceMs, delta=1.0)
            self.assertGreater(got["lastTime"], oldObservedMs)
            self.assertGreater(got["bookTime"], oldObservedMs)
            self.assertEqual(got["lastChangedTime"], oldObservedMs)
            self.assertEqual(got["bookChangedTime"], oldObservedMs)
        finally:
            with self.mod._cacheLock:
                self.mod.optionTicks.pop(code, None)

    def testMergeDoesNotRewindTimeOnOlderIncoming(self):
        """反向保护：更旧的 incoming **不得**把时间戳倒退（防回盖）。"""
        code = "90007098.SZO"
        newMs = time.time() * 1000.0
        oldMs = (time.time() - 9000) * 1000.0
        existing = {
            "last": 0.05, "bid1": 0.049, "ask1": 0.051, "vol": 10,
            "fromTdx": True, "time": newMs, "t": newMs,
        }
        incoming = {
            "last": 0.05, "bid1": 0.049, "ask1": 0.051, "vol": 10,
            "fromTdx": True, "time": oldMs, "t": oldMs, "undl": "159915",
        }
        with self.mod._cacheLock:
            self.mod.optionTicks[code] = dict(existing)
        try:
            self.mod.mergeTdxOptionTicks({code: incoming})
            got = self.mod.optionTicks[code]
            self.assertAlmostEqual(float(got.get("time") or 0), newMs, delta=1.0,
                                   msg="时间戳不得被更旧的 incoming 倒退")
        finally:
            with self.mod._cacheLock:
                self.mod.optionTicks.pop(code, None)

    def testEmptyTdxNotFreshAndNotMerged(self):
        """空盘口 fromTdx 不得锁 QMT，也不应 merge 落库。"""
        asof = date.today()
        ms = time.time() * 1000.0
        empty = {
            "fromTdx": True,
            "last": 0,
            "lastPrice": 0,
            "bid1": 0,
            "ask1": 0,
            "bidPrice": [0.0] * 5,
            "askPrice": [0.0] * 5,
            "t": ms,
            "time": ms,
            "undl": "510050",
        }
        self.assertFalse(self.mod.optionTickLooksUsable(empty, asof))
        self.assertFalse(self.mod.optionTickIsTdxFresh(empty))
        code = "10010989.SHO"
        with self.mod._cacheLock:
            self.mod.optionTicks.pop(code, None)
        try:
            n = self.mod.mergeTdxOptionTicks({code: empty})
            self.assertEqual(n, 0)
            self.assertNotIn(code, self.mod.optionTicks)
        finally:
            with self.mod._cacheLock:
                self.mod.optionTicks.pop(code, None)

    def testShoPollSkipTdxCovered(self):
        meta = {
            "10011001.SHO": {"undl": "510050.SH"},
            "10019999.SHO": {"undl": "159919.SZ"},
        }
        old = os.environ.get("DAQMT_OPT_SKIP_TDX_CHUNK")
        os.environ["DAQMT_OPT_SKIP_TDX_CHUNK"] = "1"
        try:
            out = self.mod.optionShoPollSkipTdxCovered(
                ["10011001.SHO", "10019999.SHO"], meta
            )
            self.assertEqual(out, ["10019999.SHO"])
        finally:
            if old is None:
                os.environ.pop("DAQMT_OPT_SKIP_TDX_CHUNK", None)
            else:
                os.environ["DAQMT_OPT_SKIP_TDX_CHUNK"] = old

    def testSkipTdxChunkDefaultKeepsCovered(self):
        """默认不跳过 TDX 覆盖链，以便 QMT 补量仓。"""
        meta = {
            "10011001.SHO": {"undl": "510050.SH"},
            "10019999.SHO": {"undl": "159919.SZ"},
        }
        out = self.mod.optionShoPollSkipTdxCovered(
            ["10011001.SHO", "10019999.SHO"], meta
        )
        self.assertEqual(out, ["10011001.SHO", "10019999.SHO"])

    def testMergeKeepOiWhenTdxOmitsOi(self):
        existing = {
            "last": 0.1,
            "vol": 10,
            "oi": 999,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
        }
        incoming = {
            "last": 0.1,
            "vol": 12,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
        }
        merged = self.mod.optionTickMergeKeepQty(existing, incoming)
        self.assertEqual(merged.get("vol"), 12)
        self.assertEqual(merged.get("oi"), 999)

    def testMergeKeepLargerVolAgainstLaggingTdx(self):
        existing = {
            "last": 0.1,
            "vol": 100,
            "oi": 50,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
        }
        incoming = {
            "last": 0.11,
            "vol": 80,
            "bid1": 0.09,
            "ask1": 0.12,
            "fromTdx": True,
        }
        merged = self.mod.optionTickMergeKeepQty(existing, incoming)
        self.assertEqual(merged.get("vol"), 100)
        self.assertEqual(merged.get("oi"), 50)
        self.assertEqual(merged.get("ask1"), 0.12)

    def testFillQtyTrustsQmtEvenIfVolLower(self):
        """TDX 脏高量时仍以 QMT 量仓/最新纠偏（QMT tick 新鲜）。"""
        from datetime import datetime, timedelta

        nowMs = datetime.now().timestamp() * 1000.0
        existing = {
            "last": 0.1712,
            "vol": 91062,
            "bid1": 0.1713,
            "ask1": 0.172,
            "fromTdx": True,
            "time": nowMs,
        }
        incoming = {
            "last": 0.1669,
            "vol": 85052,
            "oi": 67886,
            "openInt": 67886,
            "time": nowMs + timedelta(seconds=1).total_seconds() * 1000.0,
        }
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        self.assertEqual(patched.get("vol"), 85052)
        self.assertEqual(patched.get("oi"), 67886)
        self.assertEqual(patched.get("last"), 0.1669)
        self.assertTrue(patched.get("qtyFromQmt"))
        # 随后 TDX 合并不得回盖
        merged = self.mod.optionTickMergeKeepQty(
            patched,
            {
                "last": 0.1712,
                "vol": 91062,
                "bid1": 0.1713,
                "ask1": 0.172,
                "fromTdx": True,
            },
        )
        self.assertEqual(merged.get("vol"), 85052)
        self.assertEqual(merged.get("oi"), 67886)
        self.assertEqual(merged.get("last"), 0.1669)

    def testPayloadIgnoresTimeOnlyChange(self):
        a = {"last": 0.1, "vol": 1, "bid1": 0.09, "ask1": 0.11, "time": 1}
        b = {"last": 0.1, "vol": 1, "bid1": 0.09, "ask1": 0.11, "time": 999}
        self.assertFalse(self.mod.optionTickPayloadChanged(a, b))

    def testFillQtyUpdatesLastEvenIfQtyUnchanged(self):
        from datetime import datetime, timedelta

        nowMs = datetime.now().timestamp() * 1000.0
        existing = {
            "last": 0.10,
            "vol": 100,
            "oi": 50,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "qtyFromQmt": True,
            "time": nowMs,
        }
        incoming = {
            "last": 0.12,
            "vol": 100,
            "oi": 50,
            "openInt": 50,
            "time": nowMs + timedelta(seconds=1).total_seconds() * 1000.0,
        }
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        self.assertEqual(patched.get("last"), 0.12)
        self.assertEqual(patched.get("vol"), 100)
        self.assertTrue(patched.get("qtyFromQmt"))

    def testFillQtyFromQmtWhenTdxLacksOi(self):
        from datetime import datetime, timedelta

        nowMs = datetime.now().timestamp() * 1000.0
        existing = {
            "last": 0.1,
            "vol": 10,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "time": nowMs,
        }
        incoming = {
            "last": 0.12,
            "vol": 15,
            "oi": 888,
            "openInt": 888,
            "time": nowMs + timedelta(seconds=1).total_seconds() * 1000.0,
        }
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        self.assertEqual(patched.get("oi"), 888)
        self.assertEqual(patched.get("vol"), 15)
        self.assertEqual(patched.get("last"), 0.12)

    def testFrozenQmtTickMustNotOverwriteFreshTdx(self):
        """回归（2026-09-11 现场）：get_option_ticks 冻结旧 tick 不得回盖 TDX 新鲜值。

        现场：TDX vol=3222 被 QMT 冻结 tick 的 vol=3 覆盖，整链时间卡在 09:45:09。
        陈旧 tick 只允许补持仓量，last/vol 一律不采信，也不得标记 qtyFromQmt。
        """
        from datetime import datetime, timedelta

        nowMs = datetime.now().timestamp() * 1000.0
        frozenMs = (datetime.now() - timedelta(minutes=5)).timestamp() * 1000.0
        existing = {
            "last": 0.1311,
            "vol": 3222.0,
            "bid1": 0.1305,
            "ask1": 0.1315,
            "fromTdx": True,
            "time": nowMs,
        }
        incoming = {
            "last": 0.1855,
            "vol": 3.0,
            "oi": 6593.0,
            "openInt": 6593.0,
            "time": frozenMs,  # 冻结 5 分钟
        }
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        # TDX 新鲜值必须保留
        self.assertEqual(patched.get("vol"), 3222.0)
        self.assertEqual(patched.get("last"), 0.1311)
        self.assertEqual(patched.get("bid1"), 0.1305)
        # 持仓量允许补
        self.assertEqual(patched.get("oi"), 6593.0)
        # 不得把整条记录标成「已由 QMT 纠偏」
        self.assertFalse(patched.get("qtyFromQmt"))

    def testCrossDayQmtTickRejected(self):
        """跨日残留 tick 同样不得回盖（时间戳不在今天）。"""
        from datetime import datetime, timedelta

        nowMs = datetime.now().timestamp() * 1000.0
        prevMs = (datetime.now() - timedelta(days=1)).timestamp() * 1000.0
        existing = {
            "last": 0.05,
            "vol": 800.0,
            "bid1": 0.049,
            "ask1": 0.051,
            "fromTdx": True,
            "time": nowMs,
        }
        incoming = {"last": 0.09, "vol": 12.0, "oi": 700.0, "time": prevMs}
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        self.assertEqual(patched.get("vol"), 800.0)
        self.assertEqual(patched.get("last"), 0.05)
        self.assertEqual(patched.get("oi"), 700.0)
        self.assertFalse(patched.get("qtyFromQmt"))

    def testFreshQmtTickStillApplies(self):
        """新鲜 tick 必须照常生效，避免修过头把正常路径也堵死。"""
        from datetime import datetime, timedelta

        nowMs = datetime.now().timestamp() * 1000.0
        existing = {
            "last": 0.10,
            "vol": 100.0,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "time": nowMs,
        }
        incoming = {
            "last": 0.12,
            "vol": 120.0,
            "oi": 900.0,
            "time": nowMs + timedelta(seconds=2).total_seconds() * 1000.0,
        }
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        self.assertEqual(patched.get("vol"), 120.0)
        self.assertEqual(patched.get("last"), 0.12)
        self.assertEqual(patched.get("oi"), 900.0)
        self.assertTrue(patched.get("qtyFromQmt"))

    def testFieldClockKeepsTdxBookTimeWhenFillQty(self):
        """回归：QMT 补量仓不得用 QMT 时间覆盖 TDX 盘口时间（跨日伪实时根因）。"""
        from datetime import datetime, timedelta

        nowMs = datetime.now().timestamp() * 1000.0
        bookMs = nowMs - timedelta(seconds=5).total_seconds() * 1000.0  # TDX 盘口
        qmtMs = nowMs + timedelta(seconds=5).total_seconds() * 1000.0  # QMT tick
        existing = {
            "last": 0.10,
            "vol": 100,
            "oi": 50,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "time": bookMs,
            "t": bookMs,
        }
        incoming = {"last": 0.12, "vol": 120, "openInt": 55, "time": qmtMs}
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        # common time/t 必须仍是 TDX 盘口时钟，绝不能被 QMT 时间顶掉
        self.assertEqual(patched.get("time"), bookMs)
        self.assertEqual(patched.get("t"), bookMs)
        self.assertEqual(self.mod.optionTickTimeMs(patched), bookMs)
        # 各字段自己的时钟：成交=量=仓 走 QMT；盘口仍走 TDX
        self.assertEqual(self.mod.optionTickFieldTimeMs(patched, "last"), qmtMs)
        self.assertEqual(self.mod.optionTickFieldTimeMs(patched, "qty"), qmtMs)
        self.assertEqual(self.mod.optionTickFieldTimeMs(patched, "oi"), qmtMs)
        self.assertEqual(self.mod.optionTickFieldTimeMs(patched, "book"), bookMs)
        # 值确实被 QMT 更新
        self.assertEqual(patched.get("vol"), 120)
        self.assertEqual(patched.get("oi"), 55)
        self.assertEqual(patched.get("last"), 0.12)
        self.assertEqual(patched.get("bid1"), 0.09)

    def testFieldClockNoClockIsFailClosed(self):
        """QMT tick 无可解析时间：字段时钟写 0，不得回退到盘口时间冒充新鲜。

        同时受陈旧保护约束：无时间的 tick 视为陈旧 → last/vol 本就不采信，
        因此两个专属时钟键不存在（而非存在且为 0）。
        """
        from datetime import datetime

        bookMs = datetime.now().timestamp() * 1000.0
        existing = {
            "last": 0.10,
            "vol": 100,
            "fromTdx": True,
            "time": bookMs,
        }
        incoming = {"last": 0.12, "vol": 120}  # 无 time/t
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        # 无任何可采信字段 → 不应产生补丁
        self.assertIsNone(patched)
        self.assertTrue(self.mod.isStaleIncomingTick(0, existing))

    def testFieldClockFallbackOnlyWhenKeyAbsent(self):
        """无专属键才回退 common time；有专属键优先。"""
        from datetime import datetime

        t0 = datetime.now().timestamp() * 1000.0
        rec = {"time": t0}
        self.assertEqual(self.mod.optionTickFieldTimeMs(rec, "qty"), t0)
        rec2 = {"time": t0, "qtyTime": t0 - 60_000.0}
        self.assertEqual(self.mod.optionTickFieldTimeMs(rec2, "qty"), t0 - 60_000.0)

    def testMergeKeepQtyCarriesFieldClock(self):
        """保留旧量仓时必须连其专属时钟一起保留，否则旧值会嫁接新时间。"""
        from datetime import datetime

        qmtMs = datetime.now().timestamp() * 1000.0
        patched = {
            "last": 0.12,
            "vol": 120,
            "oi": 55,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "qtyFromQmt": True,
            "time": qmtMs,
            "qtyTime": qmtMs,
            "oiTime": qmtMs,
            "lastTime": qmtMs,
        }
        merged = self.mod.optionTickMergeKeepQty(
            patched,
            {
                "last": 0.10,
                "vol": 100,
                "bid1": 0.091,
                "ask1": 0.111,
                "fromTdx": True,
                "time": qmtMs + 1000.0,
            },
        )
        # 值保留 QMT 纠偏结果
        self.assertEqual(merged.get("vol"), 120)
        self.assertEqual(merged.get("oi"), 55)
        self.assertEqual(merged.get("last"), 0.12)
        # 时钟也跟着保留，没有被新 TDX tick 的时间顶掉
        self.assertEqual(self.mod.optionTickFieldTimeMs(merged, "qty"), qmtMs)
        self.assertEqual(self.mod.optionTickFieldTimeMs(merged, "oi"), qmtMs)
        self.assertEqual(self.mod.optionTickFieldTimeMs(merged, "last"), qmtMs)
        # 盘口换了新 TDX 档
        self.assertEqual(merged.get("bid1"), 0.091)

    def testSnapshotKeepsFieldLevelClocks(self):
        """字段级时钟必须透出到快照（LESSON-020 定稿契约）。

        历史（两次反转，都留在这里以免再走一遍）：
          ① 起初白名单缺这 4 个键 → 时钟到不了 T 板，字段级 as-of 从未生效；
          ② 直接透出 → 510050 灰行 **40%→72%**。因为当时这些时钟只在 QMT 补丁
             到达时写，而 `OPT_QTY patch n=10` 每轮只轮转 10 张 / 全链 650 张
             → 每合约约 260s 才轮一次；T 板 `pick()` 却是「键在就只认它」，
             于是 TDX 每轮都在重新观测的 510050 被改按 260s 轮转节奏判灰。
          ③ 定稿：先让 `optionTickStampTdxFieldClocks` 把 **TDX 供的字段**按每一轮
             TDX 喂数重新计时（观测时刻 = 合并墙钟），**再**透出这 4 个键。

        → 因此现在两个前提都必须成立：时钟语义对（见 testTdxMerge* 系列）+ 透出。
        """
        from datetime import datetime

        qmtMs = datetime.now().timestamp() * 1000.0
        bookMs = qmtMs - 30_000.0
        rec = {
            "lastPrice": 0.0923,
            "last": 0.0923,
            "volume": 16371,
            "openInt": 27891,
            "time": bookMs,
            "bidPrice": [0.092],
            "askPrice": [0.0925],
            "bidVol": [10],
            "askVol": [12],
            "fromTdx": True,
            "qtyFromQmt": True,
            "lastTime": qmtMs,
            "qtyTime": qmtMs,
            "oiTime": qmtMs,
            "bookTime": bookMs,
        }
        out = self.mod.compactQuoteTick(dict(rec), fillLastFromMid=False)
        for key in ("lastTime", "qtyTime", "oiTime", "bookTime"):
            self.assertIn(key, out, f"快照丢掉了字段级时钟 {key}")
        # 值必须原样（不能被改写成 common time），否则字段级 as-of 失真
        self.assertEqual(out["lastTime"], qmtMs)
        self.assertEqual(out["oiTime"], qmtMs)
        self.assertEqual(out["bookTime"], bookMs)
        # 公共时钟同时保留（供无专属键的记录回退）
        self.assertEqual(out["time"], bookMs)

    def testSnapshotMustNotFabricateZeroFieldClocks(self):
        """有专属键但值为 0 时**必须照原样透出 0** —— 0 是 fail-closed 信号。

        这条与上一条是一对：没有键 → 回退 common（上一条允许缺键）；
        **有键且为 0** → T 板 pick() 判该字段不可信（极旧）。
        若为了「好看」把 0 剔掉，等于把「这个字段的时钟不可信」这个事实藏起来。
        """
        out = self.mod.compactQuoteTick(
            {
                "lastPrice": 0.5,
                "last": 0.5,
                "volume": 100,
                "openInt": 7,
                "time": 1789364000000.0,
                "bidPrice": [0.49],
                "askPrice": [0.51],
                "bidVol": [1],
                "askVol": [1],
                "fromTdx": True,
                "lastTime": 0,
            },
            fillLastFromMid=False,
        )
        self.assertIn("lastTime", out)
        self.assertEqual(out["lastTime"], 0)

    def testTdxMergeAdvancesFieldClocksFully(self):
        """TDX 每次喂数都算「重新观测」→ 它负责的字段时钟必须按喂数节奏推进。

        现场（LESSON-020）：`lastTime` 只在 **QMT 补丁**到达时写，而 `OPT_QTY patch
        n=10` 每轮只轮转 10 张 / 全链 650 张 → 每合约约 260s 才轮一次。
        TDX 供的盘口/最新却每轮都被重新观测，时钟却不跟 → T 板把热数据判成陈旧。

        用的是**完整分支**（载荷变化），TDX 明确供了盘口与最新。
        """
        from datetime import datetime

        oldMs = datetime.now().timestamp() * 1000.0 - 120_000.0
        existing = {
            "last": 0.10,
            "vol": 100,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "time": oldMs,
            "lastTime": oldMs,
            "bookTime": oldMs,
        }
        # TDX 新一拍（载荷变化）
        incoming = {
            "last": 0.12,
            "vol": 120,
            "bid1": 0.10,
            "ask1": 0.12,
            "bidPrice": [0.10],
            "askPrice": [0.12],
            "fromTdx": True,
            "time": oldMs + 1000.0,
        }
        mergedAt = datetime.now().timestamp() * 1000.0
        out = self.mod.optionTickMergeKeepQty(existing, incoming)
        self.mod.optionTickStampTdxFieldClocks(out, existing, mergedAt)
        # TDX 供的盘口/最新：时钟推进到本次观测
        self.assertEqual(self.mod.optionTickFieldTimeMs(out, "book"), mergedAt)
        self.assertEqual(self.mod.optionTickFieldTimeMs(out, "last"), mergedAt)
        # 值确实换成了 TDX 的
        self.assertEqual(out.get("last"), 0.12)
        self.assertEqual(out.get("bid1"), 0.10)

    def testTdxMergeDoesNotAdvanceQmtOwnedFieldClocks(self):
        """量仓来自 QMT 时必须保住 QMT 时钟，不得被 TDX 的观测时刻顶掉。

        量/仓是 QMT 的槽位（`optionTickMergeKeepQty` 保留 QMT 纠偏值）。
        若这里跟着 TDX 推进，就等于用 TDX 的观测时刻给一个 TDX 没供的值背书
        —— 正是 LESSON-020 要避免的「用错基准还自信」。
        """
        from datetime import datetime

        qmtMs = datetime.now().timestamp() * 1000.0 - 300_000.0
        patched = {
            "last": 0.12,
            "vol": 120,
            "oi": 55,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "qtyFromQmt": True,
            "time": qmtMs,
            "qtyTime": qmtMs,
            "oiTime": qmtMs,
            "lastTime": qmtMs,
        }
        # TDX 新一拍：盘口变化，但量仓仍是 QMT 的值
        incoming = {
            "last": 0.10,
            "vol": 100,
            "bid1": 0.091,
            "ask1": 0.111,
            "fromTdx": True,
            "time": qmtMs + 1000.0,
        }
        mergedAt = datetime.now().timestamp() * 1000.0
        out = self.mod.optionTickMergeKeepQty(patched, incoming)
        self.mod.optionTickStampTdxFieldClocks(out, patched, mergedAt)
        # 盘口/最新是 TDX 的 → 推进
        self.assertEqual(self.mod.optionTickFieldTimeMs(out, "book"), mergedAt)
        # 量仓是 QMT 的 → 必须保持 QMT 时钟
        self.assertEqual(out.get("qtyTime"), qmtMs)
        self.assertEqual(out.get("oiTime"), qmtMs)
        self.assertEqual(mergedAt > qmtMs, True)

    def testTdxMergeFieldClockNeverRewinds(self):
        """更旧的 TDX tick 不得把字段时钟往回拨（与 LESSON-018 同一条不倒退约束）。"""
        from datetime import datetime

        nowMs = datetime.now().timestamp() * 1000.0
        existing = {
            "last": 0.12,
            "vol": 120,
            "bid1": 0.09,
            "ask1": 0.11,
            "fromTdx": True,
            "time": nowMs,
            "lastTime": nowMs,
            "bookTime": nowMs,
        }
        older = dict(existing)
        out = dict(existing)
        self.mod.optionTickStampTdxFieldClocks(out, older, nowMs - 60_000.0)
        self.assertEqual(out.get("lastTime"), nowMs)
        self.assertEqual(out.get("bookTime"), nowMs)

    def testTdxMergeClockStampCoversBothBranchShapes(self):
        """TDX 有最新价就推进 last；bookTime 仅在确有买卖盘口时推进。

        仅建形状常见于 qty 由 QMT 供给的记录 —— 恰恰是 510050 链的主要形态。
        但“只给最新价”不能虚构成“重新观测到盘口”。
        """
        from datetime import datetime

        mergedAt = datetime.now().timestamp() * 1000.0
        for extra in ({"vol": 120, "oi": 55}, {"bid1": 0.09, "ask1": 0.11}):
            existing = {"last": 0.12, "fromTdx": True, "time": mergedAt - 60_000.0}
            out = {"last": 0.12, "fromTdx": True, **extra}
            self.mod.optionTickStampTdxFieldClocks(out, existing, mergedAt)
            self.assertEqual(self.mod.optionTickFieldTimeMs(out, "last"), mergedAt)
            expectedBook = mergedAt if "bid1" in extra else 0.0
            self.assertEqual(
                self.mod.optionTickFieldTimeMs(out, "book"), expectedBook,
                f"形状 {sorted(extra)} 的盘口时钟口径错误",
            )

    def testSnapshotOmitsFieldClockKeysWhenAbsent(self):
        """源记录没有专属键时，快照不得凭空造出该键（否则会触发 fail-closed 假陈旧）。

        daqmt 对 QMT 原生记录不写 lastTime/qtyTime/oiTime；若此处补 0，
        T 板 pick() 会因「显式键存在但无效」判该字段极旧 → 正常行被涂成深灰。
        """
        from datetime import datetime

        ms = datetime.now().timestamp() * 1000.0
        out = self.mod.compactQuoteTick(
            {
                "lastPrice": 0.5,
                "last": 0.5,
                "volume": 100,
                "openInt": 7,
                "time": ms,
                "bidPrice": [0.49],
                "askPrice": [0.51],
                "bidVol": [1],
                "askVol": [1],
            },
            fillLastFromMid=False,
        )
        for key in ("lastTime", "qtyTime", "oiTime", "bookTime"):
            self.assertNotIn(key, out, f"快照凭空造出 {key}，会让 T 板 fail-closed 假陈旧")

    def testStaleTdxBookStillBlocksQmtReplace(self):
        """TDX 盘口（内容未变但时钟推进过）仍禁止 QMT 整票替换，逼走 FillQty。

        TDX 盘口本身是「新鲜」的（时钟在阈值内）；本用例验的是
        `optionTickShouldReplace` 的让路语义，与 QMT tick 陈旧保护是两件事。
        """
        from datetime import date, datetime, timedelta

        bookMs = (datetime.now() - timedelta(seconds=10)).timestamp() * 1000.0
        existing = {
            "last": 0.0923,
            "vol": 16371.0,
            "bid1": 0.092,
            "ask1": 0.0925,
            "fromTdx": True,
            "time": bookMs,
        }
        incoming = {
            "lastPrice": 0.0915,
            "volume": 16344,
            "openInt": 27891,
            "bidPrice": [0.0913],
            "askPrice": [0.0919],
            "time": bookMs + timedelta(seconds=1).total_seconds() * 1000.0,
        }
        self.assertFalse(
            self.mod.optionTickShouldReplace(existing, incoming, date.today())
        )
        patched = self.mod.optionTickFillQtyFromIncoming(existing, incoming)
        self.assertIsNotNone(patched)
        self.assertEqual(patched.get("oi"), 27891.0)
        self.assertEqual(patched.get("last"), 0.0915)
        self.assertEqual(patched.get("bid1"), 0.092)
        self.assertTrue(patched.get("qtyFromQmt"))
        self.assertTrue(patched.get("fromTdx"))

    def testQtyStopsWhenQuoteDeadYield(self):
        """全推静默时先让回调恢复；OI 等候而非继续堵塞同一 QMT 线程。"""
        self.assertFalse(self.mod.OPT_QTY_WHEN_QUOTE_DEAD)
        src = inspect.getsource(self.mod.optionQtyTdxUniverse)
        self.assertIn(".SZO", src)
        self.assertIn(".SHO", src)
        self.assertTrue(self.mod.shouldDeferOptionHeavyRpc(0, True))
        self.assertFalse(
            self.mod.shouldRunOptionQtyPatch(True, True, yieldQuote=True)
        )


class OptionQtyPriorityPlanTest(unittest.TestCase):
    """TDX 两链的 OI 补丁：ATM/高持仓/高成交优先，余量公平轮转。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    @staticmethod
    def _chain(prefix, suffix, underlying, strikes, baseVol):
        codes = []
        meta = {}
        ticks = {}
        for i, strike in enumerate(strikes):
            for cp in ("C", "P"):
                code = f"{prefix}{i}{cp}.{suffix}"
                codes.append(code)
                meta[code] = {"undl": underlying, "k": strike, "cp": cp}
                ticks[code] = {
                    "vol": baseVol + i * 10 + (1 if cp == "C" else 0),
                    "oi": baseVol * 10 + i * 100 + (1 if cp == "C" else 0),
                }
        return codes, meta, ticks

    def testShoMinuteOiGateFailsClosedAndSupportsRuntimeRollback(self):
        m = self.mod
        live = datetime(2026, 9, 23, 10, 0, 10)
        base = dict(
            now=live,
            enabled=True,
            uniqueN=7000,
            silenceSec=0.2,
            quoteHold=False,
            rpcBusy=False,
            quoteRecovery=False,
            circuitUntilMono=0.0,
            lastStartMono=0.0,
            nowMono=100.0,
            intervalSec=8.0,
        )
        self.assertTrue(m.shouldRunShoMinuteOiDownload(**base))
        for patch in (
            {"enabled": False},
            {"now": datetime(2026, 9, 23, 9, 29, 59)},
            {"uniqueN": 10},
            {"silenceSec": 8.0},
            {"quoteHold": True},
            {"rpcBusy": True},
            {"quoteRecovery": True},
            {"circuitUntilMono": 101.0},
            {"lastStartMono": 95.0},
        ):
            args = dict(base)
            args.update(patch)
            self.assertFalse(m.shouldRunShoMinuteOiDownload(**args), patch)
        self.assertFalse(m.shoMinuteOiRuntimeEnabled(True, True))
        self.assertTrue(m.shoMinuteOiRuntimeEnabled(True, False))

    def testShoMinuteOiDefaultIntervalStaysAtProvenBaselineWithoutClearGain(self):
        """7.5 秒未证实优于基线时，默认回到 8 秒，仍可用环境变量做后续 A/B。"""
        self.assertEqual(self.mod.OPT_SHO_MIN_OI_INTERVAL_SEC, 8.0)

    def testShoMinuteOiBatchPrioritizesOldestAndHonorsCooldown(self):
        m = self.mod
        codes = ["1001.SHO", "1002.SHO", "1003.SHO", "9001.SZO"]
        meta = {
            "1001.SHO": {"undl": "510050.SH"},
            "1002.SHO": {"undl": "510050.SH"},
            "1003.SHO": {"undl": "510050.SH"},
            "9001.SZO": {"undl": "159915.SZ"},
        }
        ticks = {
            "1001.SHO": {"oi": 1, "oiTime": 1_790_040_003_000, "vol": 100},
            "1002.SHO": {"oi": 1, "oiTime": 1_790_040_001_000, "vol": 10},
            "1003.SHO": {"oi": 1, "oiTime": 1_790_040_002_000, "vol": 50},
            "9001.SZO": {"oi": 1, "oiTime": 0, "vol": 999},
        }
        batch = m.planShoMinuteOiBatch(
            codes,
            ticks,
            meta,
            {"1002.SHO": 90.0},
            nowMono=100.0,
            chunkSize=2,
            cooldownSec=45.0,
        )
        self.assertEqual(batch, ["1003.SHO", "1001.SHO"])

    def testShoMinuteOiBatchUsesKnownSlotsAndOnlyTwoDiscoverySlots(self):
        m = self.mod
        codes = [f"10{i:02d}.SHO" for i in range(1, 8)]
        meta = {code: {"undl": "510050.SH"} for code in codes}
        ticks = {code: {"oi": i, "oiTime": 1_790_040_000_000 + i}
                 for i, code in enumerate(codes, 1)}
        batch = m.planShoMinuteOiBatch(
            codes,
            ticks,
            meta,
            {},
            nowMono=100.0,
            chunkSize=5,
            knownSlots=3,
            probeSlots=2,
            eligibleCodes={codes[0], codes[1], codes[2], codes[3]},
            unavailableUntil={codes[4]: 200.0},
        )
        self.assertEqual(len(batch), 5)
        self.assertEqual(set(batch[:3]), {codes[0], codes[1], codes[2]})
        self.assertNotIn(codes[4], batch)
        self.assertEqual(set(batch[3:]), {codes[5], codes[6]})

    def testShoMinuteOiBatchReturnsIdleProbeSlotsToVerifiedContracts(self):
        """未知探测池为空时，2 个探测槽必须回填已验证合约，不能只跑 8/10。"""
        m = self.mod
        codes = [f"10{i:02d}.SHO" for i in range(1, 13)]
        meta = {code: {"undl": "510050.SH"} for code in codes}
        ticks = {
            code: {"oi": i, "oiTime": 1_790_040_000_000 + i, "vol": i}
            for i, code in enumerate(codes, 1)
        }
        batch = m.planShoMinuteOiBatch(
            codes,
            ticks,
            meta,
            {},
            nowMono=100.0,
            chunkSize=10,
            knownSlots=8,
            probeSlots=2,
            eligibleCodes=set(codes),
            refillKnownSlots=True,
        )
        self.assertEqual(len(batch), 10)
        self.assertTrue(set(batch).issubset(set(codes)))
        rollback = m.planShoMinuteOiBatch(
            codes,
            ticks,
            meta,
            {},
            nowMono=100.0,
            chunkSize=10,
            knownSlots=8,
            probeSlots=2,
            eligibleCodes=set(codes),
            refillKnownSlots=False,
        )
        self.assertEqual(len(rollback), 8)

    def testShoMinuteOiLeaseShadowCountsAgeWindowsWithoutChangingState(self):
        """510050 分钟 OI 影子租约只统计年龄窗口，不改变现行卸载集合。"""
        m = self.mod
        nowMs = 1_790_040_100_000.0
        ages = {
            "fresh": 30_000,
            "under60": 50_000,
            "under75": 70_000,
            "over90": 100_000,
        }
        ticks = {}
        meta = {}
        for name, ageMs in ages.items():
            code = f"1001{name}.SHO"
            ticks[code] = {
                "oi": 100,
                "oiTime": nowMs - ageMs,
                "oiSource": "qmt_1m",
            }
            meta[code] = {"undl": "510050.SH"}
        other = "1001other.SHO"
        ticks[other] = {
            "oi": 100,
            "oiTime": nowMs - 10_000,
            "oiSource": "tdx",
        }
        meta[other] = {"undl": "510050.SH"}
        future = "1001future.SHO"
        ticks[future] = {
            "oi": 100,
            "oiTime": nowMs + 10_000,
            "oiSource": "qmt_1m",
        }
        meta[future] = {"undl": "510050.SH"}
        missing = "1001missing.SHO"
        ticks[missing] = {"oi": 100, "oiSource": "qmt_1m"}
        meta[missing] = {"undl": "510050.SH"}
        before = {code: dict(record) for code, record in ticks.items()}
        savedSuccess = dict(m._shoMinuteOiSuccessMono)
        snapshot = m.shoMinuteOiLeaseShadowSnapshot(
            ticks,
            meta,
            nowMs=nowMs,
            currentOffload={"1001fresh.SHO", "1001under60.SHO"},
        )
        self.assertEqual(snapshot["target_n"], 7)
        self.assertEqual(snapshot["source_qmt_1m_n"], 6)
        self.assertEqual(snapshot["source_other_n"], 1)
        self.assertEqual(snapshot["oi_positive_n"], 7)
        self.assertEqual(snapshot["oi_time_valid_n"], 6)
        self.assertEqual(snapshot["age_future_n"], 1)
        self.assertEqual(snapshot["age_n"], 4)
        self.assertEqual(snapshot["age_p50_sec"], 50.0)
        self.assertEqual(snapshot["age_p90_sec"], 100.0)
        self.assertEqual(snapshot["age_max_sec"], 100.0)
        self.assertEqual(snapshot["lease_n"], {"45": 1, "60": 2, "75": 3, "90": 3})
        self.assertEqual(snapshot["overlap_n"], {"45": 1, "60": 2, "75": 2, "90": 2})
        self.assertEqual(
            snapshot["lease_codes"]["45"], ("1001FRESH.SHO",)
        )
        self.assertEqual(
            snapshot["lease_codes"]["75"],
            ("1001FRESH.SHO", "1001UNDER60.SHO", "1001UNDER75.SHO"),
        )
        self.assertEqual(ticks, before)
        self.assertEqual(m._shoMinuteOiSuccessMono, savedSuccess)

    def testShoMinuteOiLeaseShadowIsDiagnosticOnly(self):
        """影子函数不能替代现有 SHO offload TTL 或改写调度开关。"""
        m = self.mod
        source = dict(m._shoMinuteOiSuccessMono)
        m._shoMinuteOiSuccessMono = {"1001.SHO": 90.0}
        try:
            snapshot = m.shoMinuteOiLeaseShadowSnapshot(
                {
                    "1001.SHO": {
                        "oi": 1,
                        "oiTime": 1_790_040_090_000.0,
                        "oiSource": "qmt_1m",
                    }
                },
                {"1001.SHO": {"undl": "510050.SH"}},
                nowMs=1_790_040_100_000.0,
                currentOffload={"1001.SHO"},
            )
            self.assertEqual(snapshot["lease_n"]["45"], 1)
            self.assertEqual(m._shoMinuteOiSuccessMono, {"1001.SHO": 90.0})
        finally:
            m._shoMinuteOiSuccessMono = source

    def testRecentSzoMinuteOiLeavesTailThenReentersWhenAged(self):
        """SZO 分钟 OI 只卸载新鲜长尾；过龄后自动回到 QMT 轮转。"""
        m = self.mod
        nowMs = 1_790_040_100_000.0
        recent = "90010001.SZO"
        stale = "90010002.SZO"
        sho = "10010001.SHO"
        meta = {
            recent: {"undl": "159915.SZ"},
            stale: {"undl": "159915.SZ"},
            sho: {"undl": "510050.SH"},
        }
        ticks = {
            recent: {
                "oi": 100,
                "oiTime": nowMs - 30_000,
                "oiSource": "qmt_1m",
            },
            stale: {
                "oi": 200,
                "oiTime": nowMs - 91_000,
                "oiSource": "qmt_1m",
            },
            sho: {
                "oi": 300,
                "oiTime": nowMs - 10_000,
                "oiSource": "qmt_1m",
            },
        }
        self.assertEqual(
            m.szoMinuteOiOffloadCodes(
                ticks, meta, nowMs=nowMs, maxAgeSec=90.0, enabled=True
            ),
            {recent},
        )
        self.assertEqual(
            m.szoMinuteOiOffloadCodes(
                ticks, meta, nowMs=nowMs, maxAgeSec=90.0, enabled=False
            ),
            set(),
        )

    def testSzoMinuteOiOffloadUsesStrictAgeWindowAndRejectsFutureBars(self):
        """严格卸载窗只接管新鲜分钟 OI，未来柱和较旧柱必须留给 QMT。"""
        m = self.mod
        nowMs = 1_790_040_100_000.0
        fresh = "90010011.SZO"
        old = "90010012.SZO"
        future = "90010013.SZO"
        meta = {
            code: {"undl": "159915.SZ"}
            for code in (fresh, old, future)
        }
        ticks = {
            fresh: {
                "oi": 100,
                "oiTime": nowMs - 30_000,
                "oiSource": "qmt_1m",
            },
            old: {
                "oi": 200,
                "oiTime": nowMs - 46_000,
                "oiSource": "qmt_1m",
            },
            future: {
                "oi": 300,
                "oiTime": nowMs + 10_000,
                "oiSource": "qmt_1m",
            },
        }
        savedGrace = m.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE
        m.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE = False
        try:
            self.assertEqual(
                m.szoMinuteOiOffloadCodes(
                    ticks, meta, nowMs=nowMs, maxAgeSec=45.0, enabled=True
                ),
                {fresh},
            )
        finally:
            m.OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE = savedGrace

    def testTailPrioritizesOldestObservedOiBeforeActivity(self):
        """同一期限内先轮转最老 OI；关闭年龄开关可恢复旧成交量排序。"""
        m = self.mod
        oldCode = "90010021.SZO"
        freshCode = "90010022.SZO"
        meta = {
            oldCode: {"undl": "159915.SZ", "exp": "2026-10-28"},
            freshCode: {"undl": "159915.SZ", "exp": "2026-10-28"},
        }
        ticks = {
            oldCode: {
                "oi": 10,
                "oiTime": 1_790_040_000_000,
                "vol": 1,
            },
            freshCode: {
                "oi": 10,
                "oiTime": 1_790_040_090_000,
                "vol": 999,
            },
        }
        saved = m.OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE
        try:
            m.OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE = True
            self.assertEqual(
                m._optionQtyTailOrderOneUnderlying(
                    [oldCode, freshCode], ticks, meta
                ),
                [oldCode, freshCode],
            )
            m.OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE = False
            self.assertEqual(
                m._optionQtyTailOrderOneUnderlying(
                    [oldCode, freshCode], ticks, meta
                ),
                [freshCode, oldCode],
            )
        finally:
            m.OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE = saved

    def testTailShareSnapshotReportsExistingFairAllocation(self):
        m = self.mod
        meta = {
            "1001.SHO": {"undl": "510050.SH"},
            "1002.SHO": {"undl": "510050.SH"},
            "9001.SZO": {"undl": "159915.SZ"},
            "other": {"undl": "510300.SH"},
        }
        self.assertEqual(
            m.optionQtyTailShareSnapshot(
                ["1001.SHO", "9001.SZO", "1002.SHO", "other"], meta
            ),
            {"510050": 2, "159915": 1, "other": 1, "total": 4},
        )

    def testTailAgeDiagnosticSeparatesSelectedAndUnservedStaleOi(self):
        """尾部取证必须如实区分全集陈旧项与本轮实际命中项，且不改输入。"""
        m = self.mod
        nowMs = 1_790_040_100_000.0
        shoOld = "10010001.SHO"
        shoFresh = "10010002.SHO"
        szoOld = "90010001.SZO"
        meta = {
            shoOld: {"undl": "510050.SH"},
            shoFresh: {"undl": "510050.SH"},
            szoOld: {"undl": "159915.SZ"},
        }
        ticks = {
            shoOld: {"oiTime": nowMs - 90_000.0},
            shoFresh: {"oiTime": nowMs - 10_000.0},
            szoOld: {"oiTime": nowMs - 120_000.0},
        }
        beforeTicks = {code: dict(rec) for code, rec in ticks.items()}
        result = m.optionQtyTailAgeDiagnosticSnapshot(
            [shoOld, shoFresh, szoOld], [shoFresh], ticks, meta,
            nowMs=nowMs, staleSec=60.0,
        )
        self.assertEqual(result["510050"]["totalN"], 2)
        self.assertEqual(result["510050"]["staleN"], 1)
        self.assertEqual(result["510050"]["selectedN"], 1)
        self.assertEqual(result["510050"]["selectedStaleN"], 0)
        self.assertEqual(result["510050"]["ageP95Sec"], 90.0)
        self.assertEqual(result["510050"]["selectedAgeP95Sec"], 10.0)
        self.assertEqual(result["159915"]["selectedN"], 0)
        self.assertEqual(result["159915"]["staleN"], 1)
        self.assertEqual(result["159915"]["ageMaxSec"], 120.0)
        self.assertEqual(ticks, beforeTicks)

    def testTailPlanImpactEstimatesStrictLeaseCostWithoutChangingPlans(self):
        """严格租约影子必须量化返还 QMT 的长尾成本，且不改计划。"""
        m = self.mod
        base = "10010001.SHO"
        returnedSho = "10010002.SHO"
        returnedSzo = "90010001.SZO"
        meta = {
            base: {"undl": "510050.SH"},
            returnedSho: {"undl": "510050.SH"},
            returnedSzo: {"undl": "159915.SZ"},
        }
        current = {
            "priority": ["p0"],
            "priority_all": ["p0"],
            "p0_need": 1,
            "p0_loaned": 0,
            "tail": [base],
            "tail_all": [base],
            "batch": ["p0", base],
        }
        strict = {
            "priority": ["p0"],
            "priority_all": ["p0"],
            "p0_need": 1,
            "p0_loaned": 0,
            "tail": [base, returnedSzo],
            "tail_all": [base, returnedSho, returnedSzo],
            "batch": ["p0", base, returnedSho],
        }
        beforeCurrent = {key: list(value) if isinstance(value, list) else value for key, value in current.items()}
        beforeStrict = {key: list(value) if isinstance(value, list) else value for key, value in strict.items()}
        impact = m.optionQtyTailPlanImpactSnapshot(
            current, strict, meta, gapSec=10.0,
        )
        self.assertTrue(impact["p0Same"])
        self.assertEqual(impact["returnedToQmtN"], 2)
        self.assertEqual(impact["current"]["tailEtaSec"], 10.0)
        self.assertEqual(impact["strict"]["tailEtaSec"], 20.0)
        self.assertEqual(impact["strict"]["share"]["159915"], 1)
        self.assertEqual(current, beforeCurrent)
        self.assertEqual(strict, beforeStrict)

    def testSzoMinuteOffloadNeverRemovesUrgentP0(self):
        """分钟路径只卸载长尾；TDX 价格/量变化形成的 P0 仍必须轮询。"""
        m = self.mod
        nowMs = 1_790_040_100_000.0
        urgent = "90010001.SZO"
        tail = "90010002.SZO"
        meta = {
            urgent: {"undl": "159915.SZ", "k": 1.9, "cp": "C"},
            tail: {"undl": "159915.SZ", "k": 2.2, "cp": "P"},
        }
        ticks = {
            urgent: {
                "oi": 100,
                "oiTime": nowMs - 20_000,
                "oiSource": "qmt_1m",
                "lastChangedTime": nowMs - 1_000,
            },
            tail: {
                "oi": 200,
                "oiTime": nowMs - 20_000,
                "oiSource": "qmt_1m",
            },
        }
        offload = m.szoMinuteOiOffloadCodes(
            ticks, meta, nowMs=nowMs, maxAgeSec=90.0, enabled=True
        )
        plan = m.planOptionQtyPatchRound(
            [urgent, tail], ticks, meta, {"159915": 1.9},
            rotate=0, roundN=0, batchSize=2, p0Slots=1, tailSlots=1,
            demandCodes=[urgent], tailExcludeCodes=offload,
        )
        self.assertEqual(plan["priority"], [urgent])
        self.assertEqual(plan["tail_all"], [])

    def testStrictSzoOffloadShrinksTailWithoutChangingBatchOrP0(self):
        """启用严格分钟卸载只减少重复长尾，不改变单批与 P0 预算。"""
        m = self.mod
        nowMs = 1_790_040_100_000.0
        codes = [f"900101{i:02d}.SZO" for i in range(20)]
        meta = {
            code: {
                "undl": "159915.SZ",
                "k": 1.0 + i * 0.1,
                "cp": "C" if i % 2 == 0 else "P",
                "exp": "2026-10-28",
            }
            for i, code in enumerate(codes)
        }
        ticks = {
            code: {
                "oi": 100 + i,
                "oiTime": nowMs - (20_000 if i < 4 else 120_000),
                "oiSource": "qmt_1m",
                "vol": 1000 - i,
            }
            for i, code in enumerate(codes)
        }
        offload = m.szoMinuteOiOffloadCodes(
            ticks, meta, nowMs=nowMs, maxAgeSec=45.0, enabled=True
        )
        without = m.planOptionQtyPatchRound(
            codes, ticks, meta, {"159915": 1.9},
            rotate=0, roundN=0, batchSize=10, p0Slots=6, tailSlots=4,
            priorityPerUnderlying=3,
        )
        withOffload = m.planOptionQtyPatchRound(
            codes, ticks, meta, {"159915": 1.9},
            rotate=0, roundN=0, batchSize=10, p0Slots=6, tailSlots=4,
            priorityPerUnderlying=3, tailExcludeCodes=offload,
        )
        self.assertEqual(len(offload), 4)
        self.assertEqual(len(without["batch"]), len(withOffload["batch"]))
        self.assertEqual(len(without["priority"]), len(withOffload["priority"]))
        self.assertLess(len(withOffload["tail_all"]), len(without["tail_all"]))
        self.assertTrue(set(offload).isdisjoint(withOffload["tail_all"]))

    def testEffectiveOiSlaIsIndependentFromRawRpcSla(self):
        """最终 OI SLA 只看合并快照覆盖与年龄，不被本轮 RPC 耗时污染。"""
        m = self.mod
        nowMs = 1_790_040_100_000.0
        codes = ["10010001.SHO", "90010001.SZO", "90010002.SZO"]
        meta = {
            codes[0]: {"undl": "510050.SH"},
            codes[1]: {"undl": "159915.SZ"},
            codes[2]: {"undl": "159915.SZ"},
        }
        ticks = {
            codes[0]: {"oi": 10, "oiTime": nowMs - 20_000},
            codes[1]: {"oi": 20, "oiTime": nowMs - 40_000},
            codes[2]: {"oi": 30, "oiTime": nowMs - 50_000},
        }
        healthy = m.optionOiEffectiveSlaSnapshot(
            codes, ticks, meta, nowMs=nowMs, targetSec=60.0
        )
        self.assertEqual(healthy["status"], "OK")
        self.assertEqual(healthy["validN"], 3)
        self.assertEqual(healthy["staleN"], 0)
        self.assertEqual(healthy["byUnderlying"]["510050"]["status"], "OK")
        self.assertEqual(healthy["byUnderlying"]["159915"]["status"], "OK")
        self.assertEqual(healthy["byUnderlying"]["159915"]["validN"], 2)
        ticks[codes[2]]["oiTime"] = nowMs - 61_000
        degraded = m.optionOiEffectiveSlaSnapshot(
            codes, ticks, meta, nowMs=nowMs, targetSec=60.0
        )
        self.assertEqual(degraded["status"], "DEGRADED")
        self.assertEqual(degraded["staleN"], 1)

    def testMinuteOffloadRemovesOnly510050TailButKeepsItsP0(self):
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.4, 2.5, 2.6, 2.7], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.8, 1.9, 2.0, 2.1], 30
        )
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.5, "159915": 1.9},
            rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=4,
            tailSlots=6,
            priorityPerUnderlying=2,
            tailExcludeUnderlyings=("510050",),
        )
        self.assertTrue(any(code.endswith(".SHO") for code in plan["priority"]))
        self.assertFalse(any(code.endswith(".SHO") for code in plan["tail_all"]))
        self.assertTrue(any(code.endswith(".SZO") for code in plan["tail_all"]))

    def testMinuteOffloadExcludesOnlyRecentlySuccessfulCodes(self):
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.4, 2.5, 2.6, 2.7], 10
        )
        plan = m.planOptionQtyPatchRound(
            aCodes,
            aTicks,
            aMeta,
            {"510050": 2.5},
            rotate=0,
            roundN=0,
            batchSize=8,
            p0Slots=2,
            tailSlots=6,
            priorityPerUnderlying=2,
            tailExcludeCodes={aCodes[-1]},
        )
        self.assertNotIn(aCodes[-1], plan["tail_all"])
        self.assertTrue(any(code.endswith(".SHO") for code in plan["tail_all"]))

    def testPartialUnknownMinuteOiDoesNotTripGlobalCircuit(self):
        m = self.mod
        first = m.shoMinuteOiCircuitDecision(
            submittedN=10,
            plannedN=10,
            elapsedSec=0.2,
            maxSubmitSec=3.0,
            eligiblePlannedN=0,
            eligibleObservedN=0,
            previousCollapseRounds=0,
        )
        self.assertEqual(first, (False, 0, "partial_allowed"))
        collapse1 = m.shoMinuteOiCircuitDecision(
            submittedN=10,
            plannedN=10,
            elapsedSec=0.2,
            maxSubmitSec=3.0,
            eligiblePlannedN=8,
            eligibleObservedN=3,
            previousCollapseRounds=0,
        )
        self.assertEqual(collapse1, (False, 1, "eligible_collapse_pending"))
        collapse2 = m.shoMinuteOiCircuitDecision(
            submittedN=10,
            plannedN=10,
            elapsedSec=0.2,
            maxSubmitSec=3.0,
            eligiblePlannedN=8,
            eligibleObservedN=3,
            previousCollapseRounds=1,
        )
        self.assertEqual(collapse2, (True, 2, "eligible_collapse"))

    def testShoMinuteOiRoundHealthTripsOnSlowOrIncompleteDownload(self):
        m = self.mod
        self.assertTrue(
            m.shoMinuteOiRoundHealthy(
                submittedN=10,
                plannedN=10,
                elapsedSec=1.9,
                maxSubmitSec=3.0,
                fileN=10,
                observedN=10,
            )
        )
        self.assertFalse(
            m.shoMinuteOiRoundHealthy(
                submittedN=10,
                plannedN=10,
                elapsedSec=3.1,
                maxSubmitSec=3.0,
                fileN=10,
                observedN=10,
            )
        )
        self.assertTrue(
            m.shoMinuteOiRoundHealthy(
                submittedN=10,
                plannedN=10,
                elapsedSec=1.9,
                maxSubmitSec=3.0,
                fileN=9,
                observedN=9,
            )
        )
        self.assertTrue(
            m.shoMinuteOiRoundHealthy(
                submittedN=10,
                plannedN=10,
                elapsedSec=1.9,
                maxSubmitSec=3.0,
                fileN=10,
                observedN=10,
                observationAgeSec=181.0,
                maxObservationAgeSec=180.0,
            )
        )

    def testShoMinuteOiRoundUsesDirectDownloadAndOnlyPatchesOi(self):
        m = self.mod
        code = "10010974.SHO"
        asof = date(2026, 9, 23)
        oldMs = datetime(2026, 9, 23, 9, 58).timestamp() * 1000.0
        newMs = datetime(2026, 9, 23, 9, 59).timestamp() * 1000.0

        class Client:
            calls = []

            def call(self, method, params, timeout_seconds=None):
                self.calls.append((method, params, timeout_seconds))
                return True

        class Xt:
            client = Client()

        savedRead = m.readQmtLocalBars
        savedDisableFile = m.OPT_SHO_MIN_OI_DISABLE_FILE
        savedTicks = dict(m.optionTicks)
        savedMeta = dict(m.optionMeta)
        savedState = (
            m._shoMinuteOiLastRoundMono,
            m._shoMinuteOiLastSuccessMono,
            m._shoMinuteOiCircuitUntilMono,
            dict(m._shoMinuteOiSubmitMono),
            dict(m._shoMinuteOiHealth),
            set(m._shoMinuteOiEligibleCodes),
            dict(m._shoMinuteOiUnavailableUntilMono),
            dict(m._shoMinuteOiSuccessMono),
            m._shoMinuteOiCollapseRounds,
            m._shoMinuteOiSeedAsof,
        )
        try:
            m.OPT_SHO_MIN_OI_DISABLE_FILE = os.path.join(
                ROOT, "__absent_sho_minute_oi_disable.flag"
            )
            m.optionTicks.clear()
            m.optionTicks[code] = {
                "fromTdx": True,
                "last": 0.02,
                "lastPrice": 0.02,
                "vol": 100,
                "oi": 30000,
                "openInt": 30000,
                "oiTime": oldMs,
                "bid1": 0.0199,
                "ask1": 0.0201,
                "bookTime": newMs,
                "time": newMs,
                "t": newMs,
            }
            m.optionMeta.clear()
            m.optionMeta[code] = {"undl": "510050.SH", "k": 3.0, "cp": "C"}
            m._shoMinuteOiLastRoundMono = 0.0
            m._shoMinuteOiLastSuccessMono = 0.0
            m._shoMinuteOiCircuitUntilMono = 0.0
            m._shoMinuteOiSubmitMono = {}
            m._shoMinuteOiEligibleCodes = set()
            m._shoMinuteOiUnavailableUntilMono = {}
            m._shoMinuteOiSuccessMono = {}
            m._shoMinuteOiCollapseRounds = 0
            m._shoMinuteOiSeedAsof = None
            m.readQmtLocalBars = lambda *_args, **_kwargs: {
                code: [
                    {
                        "time": newMs,
                        "close": 0.03,
                        "volume": 999,
                        "openInterest": 33669,
                    }
                ]
            }
            result = m.pollShoMinuteOiRound(
                m.logging.getLogger("test-sho-minute-oi"),
                Xt(),
                [code],
                asof,
                now=datetime(2026, 9, 23, 10, 0, 10),
                nowMono=100.0,
                uniqueN=7000,
                silenceSec=0.1,
            )
            self.assertTrue(result["submitted"])
            self.assertTrue(result["healthy"])
            self.assertEqual(result["applied"], 1)
            self.assertEqual(Xt.client.calls[0][0], "download_history_data2")
            self.assertEqual(Xt.client.calls[0][1]["period"], "1m")
            self.assertTrue(Xt.client.calls[0][1]["incrementally"])
            out = m.optionTicks[code]
            self.assertEqual(out["oi"], 33669)
            self.assertEqual(out["oiSource"], "qmt_1m")
            self.assertEqual(out["last"], 0.02)
            self.assertEqual(out["vol"], 100)
            self.assertEqual(out["bid1"], 0.0199)
            self.assertEqual(out["bookTime"], newMs)
        finally:
            m.readQmtLocalBars = savedRead
            m.OPT_SHO_MIN_OI_DISABLE_FILE = savedDisableFile
            m.optionTicks.clear()
            m.optionTicks.update(savedTicks)
            m.optionMeta.clear()
            m.optionMeta.update(savedMeta)
            (
                m._shoMinuteOiLastRoundMono,
                m._shoMinuteOiLastSuccessMono,
                m._shoMinuteOiCircuitUntilMono,
                submitMono,
                health,
                eligibleCodes,
                unavailableUntil,
                successMono,
                collapseRounds,
                seedAsof,
            ) = savedState
            m._shoMinuteOiSubmitMono = submitMono
            m._shoMinuteOiHealth = health
            m._shoMinuteOiEligibleCodes = eligibleCodes
            m._shoMinuteOiUnavailableUntilMono = unavailableUntil
            m._shoMinuteOiSuccessMono = successMono
            m._shoMinuteOiCollapseRounds = collapseRounds
            m._shoMinuteOiSeedAsof = seedAsof

    def testShoMinuteOiRunsBeforeTickOiAndNeverSharesOneLoop(self):
        src = inspect.getsource(self.mod._runOptionPollLoop)
        minutePos = src.index("minuteOiRound = pollShoMinuteOiRound(")
        tickPos = src.index("pollOptionQtyPatchRound(logger, xtdata, codes)")
        self.assertLess(minutePos, tickPos)
        self.assertIn("if allowQty and not minuteOiSubmitted:", src)
        pollSrc = inspect.getsource(self.mod.pollOptionQtyPatchRound)
        self.assertIn("shoOffload = shoMinuteOiOffloadCodes(startMono)", pollSrc)
        self.assertIn("szoOffloadDiag = szoMinuteOiOffloadDiagnostics(", pollSrc)
        self.assertIn("szoOffload = set(szoOffloadDiag[\"eligible\"])", pollSrc)
        self.assertIn("tailExcludeCodes=shoOffload | szoOffload", pollSrc)
        self.assertIn("raw_slo=%s", pollSrc)
        self.assertIn("effective_slo=%s", pollSrc)

    def testSzoMinuteLiveDownloadReportsEndToEndElapsedWithoutChangingBatch(self):
        """SZO 扩容前必须记录真实锁占用，且不能暗中改变既有 20 张批量。"""
        src = inspect.getsource(self.mod._maybeDownloadSzoLiveTicksChunk)
        self.assertIn("started = time.perf_counter()", src)
        self.assertIn("elapsed_s=%.3f", src)
        self.assertIn("chunk = need[:OPT_LIVE_REFRESH_CHUNK]", src)
        self.assertEqual(self.mod.OPT_LIVE_REFRESH_CHUNK, 20)

    def testPartialMinuteRoundCoolsOnlyMissingCodeAndKeepsObservedOffload(self):
        m = self.mod
        good = "10010974.SHO"
        missing = "10010975.SHO"
        asof = date(2026, 9, 23)
        oiMs = datetime(2026, 9, 23, 9, 59).timestamp() * 1000.0

        class Client:
            def call(self, *_args, **_kwargs):
                return True

        class Xt:
            client = Client()

        saved = {
            "read": m.readQmtLocalBars,
            "disable": m.OPT_SHO_MIN_OI_DISABLE_FILE,
            "ticks": dict(m.optionTicks),
            "meta": dict(m.optionMeta),
            "lastRound": m._shoMinuteOiLastRoundMono,
            "lastSuccess": m._shoMinuteOiLastSuccessMono,
            "circuit": m._shoMinuteOiCircuitUntilMono,
            "submit": dict(m._shoMinuteOiSubmitMono),
            "eligible": set(m._shoMinuteOiEligibleCodes),
            "unavailable": dict(m._shoMinuteOiUnavailableUntilMono),
            "success": dict(m._shoMinuteOiSuccessMono),
            "collapse": m._shoMinuteOiCollapseRounds,
            "seedAsof": m._shoMinuteOiSeedAsof,
            "health": dict(m._shoMinuteOiHealth),
        }
        try:
            m.OPT_SHO_MIN_OI_DISABLE_FILE = os.path.join(
                ROOT, "__absent_sho_minute_oi_disable.flag"
            )
            m.optionTicks.clear()
            m.optionTicks.update({good: {"oi": 1}, missing: {"oi": 2}})
            m.optionMeta.clear()
            m.optionMeta.update(
                {good: {"undl": "510050.SH"}, missing: {"undl": "510050.SH"}}
            )
            m._shoMinuteOiLastRoundMono = 0.0
            m._shoMinuteOiLastSuccessMono = 0.0
            m._shoMinuteOiCircuitUntilMono = 0.0
            m._shoMinuteOiSubmitMono = {}
            m._shoMinuteOiEligibleCodes = set()
            m._shoMinuteOiUnavailableUntilMono = {}
            m._shoMinuteOiSuccessMono = {}
            m._shoMinuteOiCollapseRounds = 0
            m._shoMinuteOiSeedAsof = None
            m.readQmtLocalBars = lambda *_args, **_kwargs: {
                good: [
                    {
                        "time": oiMs,
                        "close": 0.02,
                        "volume": 1,
                        "openInterest": 1234,
                    }
                ],
                missing: [],
            }
            result = m.pollShoMinuteOiRound(
                m.logging.getLogger("test-sho-minute-partial"),
                Xt(),
                [good, missing],
                asof,
                now=datetime(2026, 9, 23, 10, 0),
                nowMono=100.0,
                uniqueN=7000,
                silenceSec=0.1,
            )
            self.assertTrue(result["healthy"])
            self.assertEqual(result["reason"], "partial_allowed")
            self.assertEqual(m._shoMinuteOiCircuitUntilMono, 0.0)
            self.assertEqual(m.shoMinuteOiOffloadCodes(100.0), {good})
            self.assertGreater(m._shoMinuteOiUnavailableUntilMono[missing], 100.0)
        finally:
            m.readQmtLocalBars = saved["read"]
            m.OPT_SHO_MIN_OI_DISABLE_FILE = saved["disable"]
            m.optionTicks.clear()
            m.optionTicks.update(saved["ticks"])
            m.optionMeta.clear()
            m.optionMeta.update(saved["meta"])
            m._shoMinuteOiLastRoundMono = saved["lastRound"]
            m._shoMinuteOiLastSuccessMono = saved["lastSuccess"]
            m._shoMinuteOiCircuitUntilMono = saved["circuit"]
            m._shoMinuteOiSubmitMono = saved["submit"]
            m._shoMinuteOiEligibleCodes = saved["eligible"]
            m._shoMinuteOiUnavailableUntilMono = saved["unavailable"]
            m._shoMinuteOiSuccessMono = saved["success"]
            m._shoMinuteOiCollapseRounds = saved["collapse"]
            m._shoMinuteOiSeedAsof = saved["seedAsof"]
            m._shoMinuteOiHealth = saved["health"]

    def testMinuteOiIntervalGatePreservesLastRoundHealth(self):
        m = self.mod
        code = "10010974.SHO"
        asof = date(2026, 9, 23)
        saved = {
            "disable": m.OPT_SHO_MIN_OI_DISABLE_FILE,
            "ticks": dict(m.optionTicks),
            "meta": dict(m.optionMeta),
            "health": dict(m._shoMinuteOiHealth),
            "eligible": set(m._shoMinuteOiEligibleCodes),
            "seedAsof": m._shoMinuteOiSeedAsof,
            "lastRound": m._shoMinuteOiLastRoundMono,
        }
        try:
            m.OPT_SHO_MIN_OI_DISABLE_FILE = os.path.join(
                ROOT, "__absent_sho_minute_oi_disable.flag"
            )
            m.optionTicks.clear()
            m.optionTicks[code] = {"oi": 1234}
            m.optionMeta.clear()
            m.optionMeta[code] = {"undl": "510050.SH"}
            m._shoMinuteOiEligibleCodes = {code}
            m._shoMinuteOiSeedAsof = asof
            m._shoMinuteOiLastRoundMono = 99.0
            m._shoMinuteOiHealth = {
                "enabled": True,
                "healthy": True,
                "reason": "ok",
                "observed": 10,
            }
            result = m.pollShoMinuteOiRound(
                m.logging.getLogger("test-sho-minute-gate"),
                object(),
                [code],
                asof,
                now=datetime(2026, 9, 23, 10, 0),
                nowMono=100.0,
                uniqueN=7000,
                silenceSec=0.1,
            )
            self.assertEqual(result["reason"], "gated")
            self.assertTrue(m._shoMinuteOiHealth["healthy"])
            self.assertEqual(m._shoMinuteOiHealth["reason"], "ok")
            self.assertEqual(m._shoMinuteOiHealth["universeN"], 1)
            self.assertEqual(m._shoMinuteOiHealth["eligibleN"], 1)
        finally:
            m.OPT_SHO_MIN_OI_DISABLE_FILE = saved["disable"]
            m.optionTicks.clear()
            m.optionTicks.update(saved["ticks"])
            m.optionMeta.clear()
            m.optionMeta.update(saved["meta"])
            m._shoMinuteOiHealth = saved["health"]
            m._shoMinuteOiEligibleCodes = saved["eligible"]
            m._shoMinuteOiSeedAsof = saved["seedAsof"]
            m._shoMinuteOiLastRoundMono = saved["lastRound"]

    def testTdxOiPlanKeepsAtmAndLiquidContractsThenRotatesTail(self):
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.4, 2.5, 2.6, 2.7], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.8, 1.9, 2.0, 2.1], 30
        )
        # 一条 QMT 主导链混入输入，计划器不得抢占 TDX 两链的 OI 配额。
        qmtCode = "10009999.SHO"
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes + [qmtCode],
            {**aTicks, **bTicks, qmtCode: {"vol": 9999, "oi": 9999}},
            {**aMeta, **bMeta, qmtCode: {"undl": "510300.SH", "k": 3.8, "cp": "C"}},
            {"510050": 2.5, "159915": 1.9},
            rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=4,
            tailSlots=6,
            priorityPerUnderlying=4,
        )

        self.assertEqual(set(plan["universe"]), set(aCodes + bCodes))
        self.assertTrue(plan["priority_round"])
        self.assertIn("10001C.SHO", plan["priority"])
        self.assertIn("10001P.SHO", plan["priority"])
        self.assertIn("90001C.SZO", plan["priority"])
        self.assertIn("90001P.SZO", plan["priority"])
        self.assertLessEqual(len(plan["priority"]), 8)
        self.assertEqual(len(plan["batch"]), 10)
        self.assertEqual(len(plan["batch"]), len(set(plan["batch"])))
        self.assertNotIn(qmtCode, plan["batch"])
        self.assertGreater(plan["rotate_advance"], 0)

        tailPlan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.5, "159915": 1.9},
            rotate=plan["next_rotate"],
            roundN=1,
            batchSize=10,
            p0Slots=4,
            tailSlots=6,
            priorityPerUnderlying=4,
        )
        self.assertTrue(tailPlan["priority_round"])
        self.assertTrue(tailPlan["priority"])
        self.assertEqual(len(tailPlan["batch"]), 10)
        self.assertEqual(
            tailPlan["next_rotate"],
            (plan["next_rotate"] + tailPlan["rotate_advance"])
            % len(tailPlan["tail_all"]),
        )

    def testRoundCannotPromoteWhenDeclaredSloIsInfeasible(self):
        """RPC 返回成功不代表可升级：SLO 容量不足也必须留在保守档。"""
        m = self.mod
        self.assertFalse(
            m.optionQtyRoundHealthy(
                servedN=40,
                plannedN=40,
                elapsedSec=10.0,
                maxRpcSec=22.0,
                declaredHealthy=False,
                quoteHold=False,
                rpcBusy=False,
            )
        )
        self.assertTrue(
            m.optionQtyRoundHealthy(
                servedN=6,
                plannedN=6,
                elapsedSec=2.0,
                maxRpcSec=6.0,
                declaredHealthy=True,
                quoteHold=False,
                rpcBusy=False,
            )
        )

    def testLargestProbeRefreshesConfiguredP0EveryRound(self):
        """最大单请求探测档不能轮转 P0；尾部才允许公平轮转。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        spec = m.OPT_QTY_MODE_SPECS[-1]
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.20, "159915": 1.20},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=spec["chunkSize"] * spec["maxChunks"],
            p0Slots=spec["p0Slots"],
            tailSlots=spec["tailSlots"],
        )

        self.assertEqual(m.OPT_QTY_PRIORITY_PER_UNDL, 3)
        self.assertEqual(set(plan["priority"]), set(plan["priority_all"]))
        self.assertEqual(len(plan["priority"]), 6)
        self.assertEqual(len(plan["tail"]), 4)
        self.assertEqual(len(plan["batch"]), 10)

    def testDynamicP0LoanFillsUnusedSlotsWithoutExceedingBatch(self):
        """P0按既有SLA只需2张时，闲置4槽应借给最老长尾；总批量仍为10。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.20, "159915": 1.20},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=6,
            tailSlots=4,
            gapSec=3.0,
            p0TargetSec=10.0,
            dynamicP0Loan=True,
        )

        self.assertEqual(len(plan["priority_all"]), 6)
        self.assertEqual(plan["p0_need"], 2)
        self.assertEqual(len(plan["priority"]), 2)
        self.assertEqual(len(plan["tail"]), 8)
        self.assertEqual(plan["p0_loaned"], 4)
        self.assertEqual(len(plan["batch"]), 10)
        self.assertEqual(len(set(plan["batch"])), 10)

    def testDynamicP0LoanCanBeDisabledForInstantRollback(self):
        """环境开关关闭时必须精确恢复固定6+4配额。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.20, "159915": 1.20},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=6,
            tailSlots=4,
            gapSec=3.0,
            p0TargetSec=10.0,
            dynamicP0Loan=False,
        )

        self.assertEqual(len(plan["priority"]), 6)
        self.assertEqual(len(plan["tail"]), 4)
        self.assertEqual(plan["p0_loaned"], 0)
        self.assertEqual(len(plan["batch"]), 10)

    def testDynamicP0LoanStillCyclesEveryP0WithinTarget(self):
        """借槽后P0游标继续前进；6张候选在3秒gap下须于9秒内全覆盖。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        p0Rotate = 0
        servedP0 = set()
        for roundN in range(3):
            plan = m.planOptionQtyPatchRound(
                aCodes + bCodes,
                {**aTicks, **bTicks},
                {**aMeta, **bMeta},
                {"510050": 2.20, "159915": 1.20},
                rotate=roundN * 8,
                p0Rotate=p0Rotate,
                roundN=roundN,
                batchSize=10,
                p0Slots=6,
                tailSlots=4,
                gapSec=3.0,
                p0TargetSec=10.0,
                dynamicP0Loan=True,
            )
            self.assertEqual(len(plan["priority"]), 2)
            self.assertEqual(len(plan["tail"]), 8)
            servedP0.update(plan["priority"])
            p0Rotate = plan["next_p0_rotate"]

        self.assertEqual(servedP0, set(plan["priority_all"]))

    def testDynamicP0LoanReturnsAllSlotsWhenGapConsumesReservation(self):
        """gap增大到需要全部6个P0槽时不得继续借槽。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.20, "159915": 1.20},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=6,
            tailSlots=4,
            gapSec=10.0,
            p0TargetSec=10.0,
            dynamicP0Loan=True,
        )

        self.assertEqual(plan["p0_need"], 6)
        self.assertEqual(len(plan["priority"]), 6)
        self.assertEqual(plan["p0_loaned"], 0)
        self.assertEqual(len(plan["tail"]), 4)

    def testDynamicP0LoanGuardKeepsExtraP0Margin(self):
        """默认1.25抖动裕量使3秒gap保留3个P0槽，而不是精确卡线只留2个。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.20, "159915": 1.20},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=6,
            tailSlots=4,
            gapSec=3.0,
            p0TargetSec=10.0,
            dynamicP0Loan=True,
            p0LoanGuard=1.25,
        )

        self.assertEqual(plan["p0_need"], 3)
        self.assertEqual(len(plan["priority"]), 3)
        self.assertEqual(plan["p0_loaned"], 3)
        self.assertEqual(len(plan["tail"]), 7)

    def testBoundedP0LoanAllowsOneSlotOnlyWithinObservedAges(self):
        """安全窗口只借1槽；需求数、等待龄和P0 OI龄都必须在界内。"""
        m = self.mod
        decision = m.optionQtyP0LoanDecision(
            "probe10",
            True,
            demandN=4,
            demandWaitP95Sec=12.0,
            p0OiAgeP95Sec=18.0,
            p0OiAgeMaxSec=25.0,
            repairN=0,
            maxLoanSlots=1,
        )
        self.assertEqual(decision["cap"], 1)
        self.assertEqual(decision["reason"], "bounded_ok")
        self.assertEqual(
            m.optionQtyP0LoanDecision(
                "probe10",
                True,
                demandN=4,
                demandWaitP95Sec=12.0,
                p0OiAgeP95Sec=18.0,
                p0OiAgeMaxSec=25.0,
                repairN=0,
                maxLoanSlots=1,
            )["cap"],
            1,
        )

    def testBoundedP0LoanRejectsUnsafeDemandOrOiAge(self):
        """任一安全指标越界或缺失都不能借槽。"""
        m = self.mod
        base = dict(
            modeName="probe10",
            enabled=True,
            demandN=4,
            demandWaitP95Sec=12.0,
            p0OiAgeP95Sec=18.0,
            p0OiAgeMaxSec=25.0,
            repairN=0,
            maxLoanSlots=1,
        )
        for key, value in (
            ("demandN", 25),
            ("demandWaitP95Sec", 30.1),
            ("p0OiAgeP95Sec", 30.1),
            ("p0OiAgeMaxSec", 45.1),
            ("p0OiAgeP95Sec", None),
            ("p0OiAgeMaxSec", None),
        ):
            case = dict(base)
            case[key] = value
            self.assertEqual(
                m.optionQtyP0LoanDecision(**case)["cap"],
                0,
                msg=f"unsafe {key}={value!r} must not loan",
            )
        self.assertEqual(
            m.optionQtyP0LoanDecision(**{**base, "repairN": 1})["cap"],
            0,
        )
        self.assertEqual(
            m.optionQtyP0LoanDecision(**{**base, "enabled": False})["cap"],
            0,
        )
        self.assertEqual(
            m.optionQtyP0LoanDecision(**{**base, "modeName": "probe8"})["cap"],
            0,
        )

    def testBoundedP0LoanSecondSlotNeedsExplicitStrictGate(self):
        """第二槽默认关闭；显式开启也必须满足更严格指标。"""
        m = self.mod
        kwargs = dict(
            modeName="probe10",
            enabled=True,
            demandN=4,
            demandWaitP95Sec=12.0,
            p0OiAgeP95Sec=18.0,
            p0OiAgeMaxSec=25.0,
            repairN=0,
            maxLoanSlots=2,
        )
        self.assertEqual(m.optionQtyP0LoanDecision(**kwargs)["cap"], 1)
        strict = {
            **kwargs,
            "allowTwoSlots": True,
            "secondDemandMaxN": 8,
            "secondWaitMaxSec": 10.0,
            "secondOiP95MaxSec": 20.0,
            "secondOiMaxSec": 30.0,
            "demandWaitP95Sec": 8.0,
            "p0OiAgeP95Sec": 12.0,
            "p0OiAgeMaxSec": 18.0,
        }
        self.assertEqual(m.optionQtyP0LoanDecision(**strict)["cap"], 2)

    def testBoundedP0LoanCapIsAppliedWithoutChangingBatchSize(self):
        """计划器硬限制借槽数；总请求仍为10且P0至少保留5席。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.20, "159915": 1.20},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=6,
            tailSlots=4,
            gapSec=3.0,
            p0TargetSec=10.0,
            dynamicP0Loan=True,
            p0LoanCap=1,
        )
        self.assertEqual(plan["p0_loaned"], 1)
        self.assertEqual(len(plan["priority"]), 5)
        self.assertEqual(len(plan["tail"]), 5)
        self.assertEqual(len(plan["batch"]), 10)

    def testDynamicP0LoanRunsOnlyInProbe10Mode(self):
        """base6/probe8保持固定P0；仅健康probe10可借槽。"""
        m = self.mod
        self.assertFalse(m.shouldUseOptionQtyDynamicP0Loan("base6", True))
        self.assertFalse(m.shouldUseOptionQtyDynamicP0Loan("probe8", True))
        self.assertTrue(m.shouldUseOptionQtyDynamicP0Loan("probe10", True))
        self.assertFalse(m.shouldUseOptionQtyDynamicP0Loan("probe10", False))

    def testDynamicP0LoanStopsWhenUrgentOrRepairSetCanChurn(self):
        """价量紧急集会换入换出；存在紧急/修复候选时必须恢复原6张P0。"""
        m = self.mod
        self.assertFalse(m.shouldUseOptionQtyDynamicP0Loan(
            "probe10", True, demandN=1,
        ))
        self.assertFalse(m.shouldUseOptionQtyDynamicP0Loan(
            "probe10", True, repairN=1,
        ))
        self.assertTrue(m.shouldUseOptionQtyDynamicP0Loan(
            "probe10", True, demandN=0, repairN=0,
        ))

    def testDynamicP0LoanDoesNotHideP0OverCapacity(self):
        """P0需求超过6时须保留真实need供DEGRADED判断，不能截断成6。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(20)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(20)], 30
        )
        held = set(aCodes[:5] + bCodes[:5])
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.20, "159915": 1.20},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=10,
            p0Slots=6,
            tailSlots=4,
            heldCodes=held,
            gapSec=10.0,
            p0TargetSec=10.0,
            dynamicP0Loan=True,
        )

        self.assertGreater(plan["p0_need"], 6)
        self.assertEqual(len(plan["priority"]), 6)
        self.assertEqual(plan["p0_loaned"], 0)

    def testOpenPositionsAreNeverDemotedIntoTailWhenTheyExceedP0Slots(self):
        """P0 槽位不够时必须显式降级，不能把开放仓位悄悄塞进长尾。"""
        m = self.mod
        aCodes, aMeta, aTicks = self._chain(
            "1000", "SHO", "510050.SH", [2.10 + i * 0.01 for i in range(4)], 10
        )
        bCodes, bMeta, bTicks = self._chain(
            "9000", "SZO", "159915.SZ", [1.10 + i * 0.01 for i in range(4)], 30
        )
        held = set(aCodes[:4])
        plan = m.planOptionQtyPatchRound(
            aCodes + bCodes,
            {**aTicks, **bTicks},
            {**aMeta, **bMeta},
            {"510050": 2.12, "159915": 1.12},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=40,
            p0Slots=6,
            tailSlots=34,
            priorityPerUnderlying=3,
            heldCodes=held,
        )

        self.assertTrue(held.issubset(set(plan["priority_all"])))
        self.assertFalse(held.intersection(plan["tail_all"]))
        self.assertGreater(len(plan["priority_all"]), 6)

    def testStaleQmtLedOiRepairGetsHealthyModeP0Slots(self):
        """QMT 主导链只在 OI 真缺失时抢修，不能因无回调挤占 TDX 两链。"""
        m = self.mod
        self.assertEqual(m.OPT_QTY_QMT_REPAIR_PER_UNDL, 4)
        self.assertEqual(m.OPT_QTY_MODE_SPECS[-1]["p0Slots"], 6)
        nowMs = datetime.now().timestamp() * 1000.0
        qmtCodes = [
            "10030001.SHO", "10030002.SHO",
            "10050001.SHO", "10050002.SHO",
            "10080001.SHO", "10080002.SHO",
        ]
        meta = {
            code: {
                "undl": undl,
                "k": strike,
                "cp": "C" if code.endswith("01.SHO") else "P",
            }
            for code, undl, strike in (
                ("10030001.SHO", "510300.SH", 3.8),
                ("10030002.SHO", "510300.SH", 3.8),
                ("10050001.SHO", "510500.SH", 6.2),
                ("10050002.SHO", "510500.SH", 6.2),
                ("10080001.SHO", "588000.SH", 1.1),
                ("10080002.SHO", "588000.SH", 1.1),
            )
        }
        ticks = {
            code: {"oi": 1, "oiTime": nowMs - 60_000.0, "vol": 100}
            for code in qmtCodes
        }
        repair = m.optionQtyQmtOiRepairCodes(
            qmtCodes,
            ticks,
            meta,
            {"510300": 3.8, "510500": 6.2, "588000": 1.1},
            nowMs=nowMs,
        )
        # callback 的无变化不等于 OI 不可用；有值即使最后变动较早，也不能抢 P0。
        self.assertEqual(repair, [])
        for code in qmtCodes:
            ticks[code].pop("oi")
            ticks[code].pop("oiTime")
        repair = m.optionQtyQmtOiRepairCodes(
            qmtCodes,
            ticks,
            meta,
            {"510300": 3.8, "510500": 6.2, "588000": 1.1},
            nowMs=nowMs,
        )
        self.assertEqual(set(repair), set(qmtCodes))
        plan = m.planOptionQtyPatchRound(
            [], {}, {}, {},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=40,
            p0Slots=m.OPT_QTY_MODE_SPECS[-1]["p0Slots"],
            tailSlots=m.OPT_QTY_MODE_SPECS[-1]["tailSlots"],
            extraPriorityCodes=repair,
        )
        self.assertEqual(set(plan["priority_all"]), set(qmtCodes))
        self.assertEqual(set(plan["priority"]), set(qmtCodes))
        self.assertEqual(plan["tail"], [])

    def testTdxPriceOrVolumeChangePromotesOiDemandAheadOfStaticCandidates(self):
        """TDX 已发现成交/价格变化而 OI 未复核的合约必须进入 P0。"""
        m = self.mod
        nowMs = datetime.now().timestamp() * 1000.0
        codes = ["10010001.SHO", "10010002.SHO", "90010001.SZO", "90010002.SZO"]
        meta = {
            "10010001.SHO": {"undl": "510050.SH", "k": 3.0, "exp": "2026-09-23"},
            "10010002.SHO": {"undl": "510050.SH", "k": 3.1, "exp": "2026-10-28"},
            "90010001.SZO": {"undl": "159915.SZ", "k": 1.9, "exp": "2026-09-23"},
            "90010002.SZO": {"undl": "159915.SZ", "k": 2.0, "exp": "2026-10-28"},
        }
        ticks = {
            code: {"oi": 100, "oiTime": nowMs - 20_000.0, "vol": 1}
            for code in codes
        }
        ticks["10010002.SHO"]["lastChangedTime"] = nowMs - 1_000.0
        ticks["90010002.SZO"]["qtyChangedTime"] = nowMs - 2_000.0
        demand = m.optionQtyTdxDemandCodes(codes, ticks, meta)
        self.assertEqual(demand, ["10010002.SHO", "90010002.SZO"])
        plan = m.planOptionQtyPatchRound(
            codes,
            ticks,
            meta,
            {"510050": 3.0, "159915": 1.9},
            rotate=0,
            p0Rotate=0,
            roundN=0,
            batchSize=2,
            p0Slots=2,
            tailSlots=0,
            demandCodes=demand,
        )
        self.assertEqual(set(plan["priority"]), set(demand))

    def testOptionQtyDiagnosticSplitsRealSourcesWithoutChangingPlan(self):
        """诊断须拆清价/量/ATM/持仓/修复，且不得改变需求或计划结果。"""
        m = self.mod
        nowMs = 1_800_000_000_000.0
        codes = [
            "10010001.SHO", "10010002.SHO", "10010003.SHO",
            "90010001.SZO", "90010002.SZO", "90010003.SZO",
        ]
        meta = {
            "10010001.SHO": {"undl": "510050.SH", "k": 3.0, "cp": "C"},
            "10010002.SHO": {"undl": "510050.SH", "k": 3.1, "cp": "P"},
            "10010003.SHO": {"undl": "510050.SH", "k": 3.2, "cp": "C"},
            "90010001.SZO": {"undl": "159915.SZ", "k": 1.9, "cp": "C"},
            "90010002.SZO": {"undl": "159915.SZ", "k": 2.0, "cp": "P"},
            "90010003.SZO": {"undl": "159915.SZ", "k": 2.1, "cp": "C"},
        }
        ticks = {
            code: {"oi": 100, "oiTime": nowMs - 20_000, "vol": 10 + index}
            for index, code in enumerate(codes)
        }
        ticks["10010001.SHO"]["lastChangedTime"] = nowMs - 9_000
        ticks["10010002.SHO"]["qtyChangedTime"] = nowMs - 8_000
        ticks["90010001.SZO"].update({
            "oiTime": 0,
            "lastChangedTime": nowMs - 7_000,
            "qtyChangedTime": nowMs - 6_000,
        })
        held = {"10010003.SHO"}
        repair = ["10030001.SHO"]
        repairMeta = {"10030001.SHO": {"undl": "510300.SH", "k": 4.0}}
        demandBefore = m.optionQtyTdxDemandCodes(codes, ticks, meta)
        planBefore = m.planOptionQtyPatchRound(
            codes, ticks, meta, {"510050": 3.0, "159915": 2.0},
            rotate=0, p0Rotate=0, roundN=0, batchSize=10,
            p0Slots=6, tailSlots=4, heldCodes=held,
            demandCodes=demandBefore, extraPriorityCodes=repair,
        )
        pending = {}
        diag = m.optionQtyP0DiagnosticSnapshot(
            planBefore,
            demandBefore,
            repair,
            held,
            ticks,
            {**meta, **repairMeta},
            {"510050": 3.0, "159915": 2.0},
            nowMs=nowMs,
            pendingSinceMs=pending,
        )

        self.assertEqual(diag["demand"], 3)
        self.assertEqual(diag["demand_last_only"], 1)
        self.assertEqual(diag["demand_qty_only"], 1)
        self.assertEqual(diag["demand_both"], 1)
        self.assertEqual(diag["demand_missing_oi"], 1)
        self.assertEqual(diag["p0_held"], 1)
        self.assertGreaterEqual(diag["p0_atm"], 1)
        self.assertEqual(diag["qmt_repair"], 1)
        self.assertEqual(diag["demand_wait_max_sec"], 0.0)
        # 借槽安全门读取的是本轮实际 P0 的 OI 年龄，而不是 priority_all
        # 长尾候选；新挂牌/缺失 OI 必须单独计数，不能伪造为0秒。
        self.assertIsNotNone(diag["p0_oi_age_p95_sec"])
        self.assertGreaterEqual(diag["p0_oi_age_p95_sec"], 20.0)
        self.assertGreaterEqual(diag["p0_oi_missing"], 1)
        self.assertEqual(demandBefore, m.optionQtyTdxDemandCodes(codes, ticks, meta))
        self.assertEqual(
            planBefore,
            m.planOptionQtyPatchRound(
                codes, ticks, meta, {"510050": 3.0, "159915": 2.0},
                rotate=0, p0Rotate=0, roundN=0, batchSize=10,
                p0Slots=6, tailSlots=4, heldCodes=held,
                demandCodes=demandBefore, extraPriorityCodes=repair,
            ),
        )

        # 同一待处理集合持续存在时，等待年龄必须增长；最新价继续变化也不能归零。
        ticks["10010001.SHO"]["lastChangedTime"] = nowMs + 2_000
        diagLater = m.optionQtyP0DiagnosticSnapshot(
            planBefore,
            demandBefore,
            repair,
            held,
            ticks,
            {**meta, **repairMeta},
            {"510050": 3.0, "159915": 2.0},
            nowMs=nowMs + 5_000,
            pendingSinceMs=pending,
        )
        self.assertEqual(diagLater["demand_wait_max_sec"], 5.0)

    def testOptionQtyDiagnosticLogExecutesPollPathWithoutChangingScheduling(self):
        """真实 poll 路径须能限频写诊断，且空计划仍保持零 RPC。"""
        m = self.mod
        code = "10010001.SHO"
        nowMs = datetime.now().timestamp() * 1000.0
        saved = {
            "ticks": dict(m.optionTicks),
            "meta": dict(m.optionMeta),
            "held": m.loadOpenOptionQtyPriorityCodes,
            "repair": m.optionQtyQmtOiRepairCodes,
            "demand": m.optionQtyTdxDemandCodes,
            "sho": m.shoMinuteOiOffloadCodes,
            "szo": m.szoMinuteOiOffloadCodes,
            "plan": m.planOptionQtyPatchRound,
            "diagSec": m.OPT_QTY_P0_DIAG_LOG_SEC,
            "diagLast": m._qtyP0DiagLastLogMono,
            "szoDiagSec": m.OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC,
            "szoDiagLast": m._szoOiOffloadDiagLastLogMono,
            "diagPending": dict(m._qtyP0DiagPendingSinceMs),
        }
        try:
            m.optionTicks.clear()
            m.optionTicks[code] = {
                "oi": 100,
                "oiTime": nowMs - 20_000,
                "lastChangedTime": nowMs - 1_000,
            }
            m.optionMeta.clear()
            m.optionMeta[code] = {"undl": "510050.SH", "k": 3.0}
            m.loadOpenOptionQtyPriorityCodes = lambda _logger: frozenset()
            m.optionQtyQmtOiRepairCodes = lambda *_args, **_kwargs: []
            m.optionQtyTdxDemandCodes = lambda *_args, **_kwargs: [code]
            m.shoMinuteOiOffloadCodes = lambda *_args, **_kwargs: set()
            m.szoMinuteOiOffloadCodes = lambda *_args, **_kwargs: set()
            m.planOptionQtyPatchRound = lambda *_args, **_kwargs: {
                "universe": [code],
                "priority_all": [code],
                "priority": [],
                "tail_all": [],
                "tail": [],
                "batch": [],
                "p0_need": 1,
                "p0_loaned": 0,
            }
            m.OPT_QTY_P0_DIAG_LOG_SEC = 0.001
            m._qtyP0DiagLastLogMono = 0.0
            m.OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC = 0.001
            m._szoOiOffloadDiagLastLogMono = 0.0
            m._qtyP0DiagPendingSinceMs.clear()
            logger = m.logging.getLogger("test-opt-qty-diag-poll")
            with self.assertLogs(logger, level="INFO") as captured:
                result = m.pollOptionQtyPatchRound(logger, object(), [code])
            self.assertEqual(result, 0)
            self.assertTrue(any("OPT_QTY_DIAG demand=1" in line for line in captured.output))
            self.assertGreater(m._qtyP0DiagLastLogMono, 0.0)
            self.assertTrue(any("OPT_SZO_OI_DIAG enabled=1 target=0" in line for line in captured.output))
            self.assertGreater(m._szoOiOffloadDiagLastLogMono, 0.0)
        finally:
            m.optionTicks.clear()
            m.optionTicks.update(saved["ticks"])
            m.optionMeta.clear()
            m.optionMeta.update(saved["meta"])
            m.loadOpenOptionQtyPriorityCodes = saved["held"]
            m.optionQtyQmtOiRepairCodes = saved["repair"]
            m.optionQtyTdxDemandCodes = saved["demand"]
            m.shoMinuteOiOffloadCodes = saved["sho"]
            m.szoMinuteOiOffloadCodes = saved["szo"]
            m.planOptionQtyPatchRound = saved["plan"]
            m.OPT_QTY_P0_DIAG_LOG_SEC = saved["diagSec"]
            m._qtyP0DiagLastLogMono = saved["diagLast"]
            m.OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC = saved["szoDiagSec"]
            m._szoOiOffloadDiagLastLogMono = saved["szoDiagLast"]
            m._qtyP0DiagPendingSinceMs.clear()
            m._qtyP0DiagPendingSinceMs.update(saved["diagPending"])

    def testTailInterleavesExpiryBucketsAndRanksActivityWithinBucket(self):
        """远近月都必须轮到；同一期限内先服务高成交/缺 OI 合约。"""
        m = self.mod
        codes = ["10010001.SHO", "10010002.SHO", "10010003.SHO", "10010004.SHO"]
        meta = {
            "10010001.SHO": {"exp": "2026-09-23"},
            "10010002.SHO": {"exp": "2026-09-23"},
            "10010003.SHO": {"exp": "2026-10-28"},
            "10010004.SHO": {"exp": "2026-10-28"},
        }
        ticks = {
            "10010001.SHO": {"vol": 1, "oi": 1},
            "10010002.SHO": {"vol": 9, "oi": 1},
            "10010003.SHO": {"vol": 2, "oi": 1},
            "10010004.SHO": {"vol": 8, "oi": 1},
        }
        self.assertEqual(
            m._optionQtyTailOrder(codes, ticks, meta),
            ["10010002.SHO", "10010004.SHO", "10010001.SHO", "10010003.SHO"],
        )

    def testTailKeepsBothTdxUnderlyingsInEachProbe10TailBatch(self):
        """同到期月、159915 更活跃时也不能吞掉 510050 的尾部配额。"""
        m = self.mod
        aCodes = [f"100200{i:02d}.SHO" for i in range(8)]
        bCodes = [f"900200{i:02d}.SZO" for i in range(8)]
        codes = aCodes + bCodes
        meta = {
            **{code: {"undl": "510050.SH", "exp": "2026-10-28"} for code in aCodes},
            **{code: {"undl": "159915.SZ", "exp": "2026-10-28"} for code in bCodes},
        }
        ticks = {
            **{code: {"vol": 1, "oi": 1} for code in aCodes},
            **{code: {"vol": 100, "oi": 100} for code in bCodes},
        }
        order = m._optionQtyTailOrder(codes, ticks, meta)
        self.assertEqual(
            [m.undlSixFromMeta(meta[code]) for code in order[:4]],
            ["510050", "159915", "510050", "159915"],
        )

    def testTdxDemandPrioritizesOldestUnobservedOiBeforeNewestChange(self):
        """持续活跃的最新变动不能永久压住更久未复核 OI 的合约。"""
        m = self.mod
        nowMs = datetime.now().timestamp() * 1000.0
        codes = ["10030001.SHO", "10030002.SHO", "90030001.SZO", "90030002.SZO"]
        meta = {
            "10030001.SHO": {"undl": "510050.SH"},
            "10030002.SHO": {"undl": "510050.SH"},
            "90030001.SZO": {"undl": "159915.SZ"},
            "90030002.SZO": {"undl": "159915.SZ"},
        }
        ticks = {
            "10030001.SHO": {"oiTime": nowMs - 1_000.0, "lastChangedTime": nowMs - 100.0},
            "10030002.SHO": {"oiTime": nowMs - 30_000.0, "lastChangedTime": nowMs - 20_000.0},
            "90030001.SZO": {"oiTime": nowMs - 1_000.0, "qtyChangedTime": nowMs - 100.0},
            "90030002.SZO": {"oiTime": nowMs - 30_000.0, "qtyChangedTime": nowMs - 20_000.0},
        }
        self.assertEqual(
            m.optionQtyTdxDemandCodes(codes, ticks, meta),
            ["10030002.SHO", "90030002.SZO", "10030001.SHO", "90030001.SZO"],
        )

    def testDeadPushConfirmationSuppressesOiUntilRecoveryDecision(self):
        """空缓存强制重订后的确认窗内，不得再用 OI 重型 RPC 挤占恢复。"""
        m = self.mod
        self.assertTrue(
            m.shouldEnterOptionPushFallback(
                396, 396, 0, True, True,
                sinceForceSec=m.OPT_QUOTE_SUB_FORCE_CONFIRM_SEC + 0.1,
            )
        )
        self.assertFalse(
            m.shouldEnterOptionPushFallback(396, 396, 1, True, True, sinceForceSec=999.0)
        )
        self.assertFalse(
            m.shouldEnterOptionPushFallback(396, 396, 0, True, False, sinceForceSec=999.0)
        )
        self.assertFalse(
            m.shouldRunOptionQtyPatch(True, True, quoteRecovery=True)
        )

    def testDeadPushRecoveryNeedsStableFreshWindow(self):
        """瞬时回调不能解除熔断，否则会重新落入 force 重订循环。"""
        m = self.mod
        self.assertFalse(
            m.shouldExitOptionPushFallback(
                182, sinceRecoverySec=0.0,
            )
        )
        self.assertFalse(
            m.shouldExitOptionPushFallback(
                182, sinceRecoverySec=m.OPT_QUOTE_SUB_RECOVER_CONFIRM_SEC - 0.1,
            )
        )
        self.assertTrue(
            m.shouldExitOptionPushFallback(
                182, sinceRecoverySec=m.OPT_QUOTE_SUB_RECOVER_CONFIRM_SEC,
            )
        )
        self.assertFalse(
            m.shouldExitOptionPushFallback(0, sinceRecoverySec=999.0)
        )

    def testProbeLadderUsesOneRequestAndKeepsP0AsSafetyGate(self):
        """6/8/10 均为单请求探测；尾部 SLA 未达不得阻止受控探测升级。"""
        m = self.mod
        self.assertEqual(
            [(spec["chunkSize"], spec["maxChunks"]) for spec in m.OPT_QTY_MODE_SPECS],
            [(6, 1), (8, 1), (10, 1)],
        )
        self.assertTrue(
            m.optionQtyRoundProbeSafe(
                servedN=8, plannedN=8, elapsedSec=2.5, maxRpcSec=3.5,
                p0Capacity=True, quoteHold=False, rpcBusy=False,
            )
        )
        self.assertFalse(
            m.optionQtyRoundProbeSafe(
                servedN=8, plannedN=8, elapsedSec=3.6, maxRpcSec=3.5,
                p0Capacity=True, quoteHold=False, rpcBusy=False,
            )
        )
        self.assertFalse(
            m.optionQtyRoundProbeSafe(
                servedN=8, plannedN=8, elapsedSec=2.5, maxRpcSec=3.5,
                p0Capacity=False, quoteHold=False, rpcBusy=False,
            )
        )


class OptionQuoteChangedClockTest(unittest.TestCase):
    """最后观测与最后变动不能混用（页面需同时披露）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testTdxReobservationAdvancesObservedButNotChangedClock(self):
        m = self.mod
        oldMs = datetime.now().timestamp() * 1000.0 - 30_000.0
        observedMs = oldMs + 20_000.0
        existing = {
            "lastPrice": 0.1234,
            "last": 0.1234,
            "bid1": 0.1230,
            "ask1": 0.1235,
            "fromTdx": True,
            "time": oldMs,
            "lastTime": oldMs,
            "bookTime": oldMs,
            "lastChangedTime": oldMs,
            "bookChangedTime": oldMs,
        }
        out = dict(existing)
        m.optionTickStampTdxFieldClocks(out, existing, observedMs)
        m.optionTickStampChangedFieldClocks(out, existing, observedMs)

        self.assertEqual(m.optionTickFieldTimeMs(out, "last"), observedMs)
        self.assertEqual(m.optionTickFieldTimeMs(out, "book"), observedMs)
        self.assertEqual(m.optionTickFieldChangedTimeMs(out, "last"), oldMs)
        self.assertEqual(m.optionTickFieldChangedTimeMs(out, "book"), oldMs)

    def testStaleQmtOiKeepsObservedClockButUsesSourceEventForChangedClock(self):
        """允许补陈旧 OI 时，不能把旧观测时钟伪装成最后变动时刻。"""
        m = self.mod
        nowMs = datetime.now().timestamp() * 1000.0
        priorMs = nowMs - 300_000.0
        qmtEventMs = nowMs - 120_000.0
        existing = {
            "fromTdx": True,
            "time": nowMs,
            "oi": 100.0,
            "openInt": 100.0,
            "oiTime": priorMs,
            "oiChangedTime": priorMs,
        }
        out = m.optionTickFillQtyFromIncoming(
            existing, {"openInt": 120.0, "time": qmtEventMs}
        )

        self.assertIsNotNone(out)
        self.assertEqual(out["oi"], 120.0)
        self.assertEqual(m.optionTickFieldTimeMs(out, "oi"), priorMs)
        self.assertEqual(m.optionTickFieldChangedTimeMs(out, "oi"), qmtEventMs)

    def testTdxMergeKeepsQmtSourcesForQmtCarriedFields(self):
        """TDX 新盘口不能把 QMT 保留的最新/量/仓来源重写成 TDX。"""
        m = self.mod
        code = "10010971.SHO"
        # 仍在 QMT 主导让位阈值内的值必须保留；超过阈值的接管路径另有回归。
        oldMs = (datetime.now().timestamp() - 5.0) * 1000.0
        incomingMs = oldMs + 1_000.0
        existing = {
            "last": 0.1234,
            "lastPrice": 0.1234,
            "vol": 1200,
            "volume": 1200,
            "oi": 54321,
            "openInt": 54321,
            "bid1": 0.1230,
            "ask1": 0.1235,
            "fromTdx": True,
            "qtyFromQmt": True,
            "lastSource": "qmt",
            "qtySource": "qmt",
            "oiSource": "qmt",
            "bookSource": "tdx",
            "time": oldMs,
            "lastTime": oldMs,
            "qtyTime": oldMs,
            "oiTime": oldMs,
            "bookTime": oldMs,
        }
        incoming = {
            "last": 0.12,
            "lastPrice": 0.12,
            "vol": 1100,
            "volume": 1100,
            "bid1": 0.119,
            "ask1": 0.121,
            "time": incomingMs,
            "undl": "510050",
        }
        with m._cacheLock:
            m.optionTicks[code] = dict(existing)
        try:
            self.assertGreaterEqual(m.mergeTdxOptionTicks({code: incoming}), 1)
            got = m.optionTicks[code]
            self.assertEqual(got["last"], existing["last"])
            self.assertEqual(got["vol"], existing["vol"])
            self.assertEqual(got["oi"], existing["oi"])
            self.assertEqual(got["lastSource"], "qmt")
            self.assertEqual(got["qtySource"], "qmt")
            self.assertEqual(got["oiSource"], "qmt")
            self.assertEqual(got["bookSource"], "tdx")
            self.assertEqual(got["lastTime"], oldMs)
            self.assertEqual(got["qtyTime"], oldMs)
            self.assertEqual(got["oiTime"], oldMs)
        finally:
            with m._cacheLock:
                m.optionTicks.pop(code, None)

    def testTdxTakesOverStaleQmtLastAndVolumeButNeverFakesOi(self):
        """QMT 回调超时后，TDX 可接管 last/vol；TDX 缺 OI 仍保留其真实陈旧度。"""
        m = self.mod
        code = "10010972.SHO"
        nowMs = datetime.now().timestamp() * 1000.0
        oldMs = nowMs - (m.QMT_LED_MAX_STALE_SEC + 5.0) * 1000.0
        existing = {
            "last": 0.1234,
            "lastPrice": 0.1234,
            "vol": 1200,
            "volume": 1200,
            "oi": 54321,
            "openInt": 54321,
            "bid1": 0.1230,
            "ask1": 0.1235,
            "fromTdx": True,
            "qtyFromQmt": True,
            "lastSource": "qmt",
            "qtySource": "qmt",
            "oiSource": "qmt",
            "bookSource": "tdx",
            "time": oldMs,
            "lastTime": oldMs,
            "qtyTime": oldMs,
            "oiTime": oldMs,
            "bookTime": oldMs,
        }
        incoming = {
            "last": 0.12,
            "lastPrice": 0.12,
            "vol": 1210,
            "volume": 1210,
            "bid1": 0.119,
            "ask1": 0.121,
            "time": nowMs,
            "undl": "510300",
        }
        with m._cacheLock:
            m.optionTicks[code] = dict(existing)
        try:
            self.assertGreaterEqual(m.mergeTdxOptionTicks({code: incoming}), 1)
            got = m.optionTicks[code]
            self.assertEqual(got["last"], incoming["last"])
            self.assertEqual(got["vol"], incoming["vol"])
            self.assertEqual(got["lastSource"], "tdx")
            self.assertEqual(got["qtySource"], "tdx")
            self.assertEqual(got["oi"], existing["oi"])
            self.assertEqual(got["oiSource"], "qmt")
            self.assertEqual(got["oiTime"], oldMs)
            self.assertGreater(got["lastTime"], oldMs)
            self.assertGreater(got["qtyTime"], oldMs)
        finally:
            with m._cacheLock:
                m.optionTicks.pop(code, None)

    def testTdxTakesOverStaleQmtSourceWhenValuesAreUnchanged(self):
        """数值相同不能阻止过期 QMT 的 last/vol 来源交给 TDX。"""
        m = self.mod
        code = "10010973.SHO"
        nowMs = datetime.now().timestamp() * 1000.0
        oldMs = nowMs - (m.QMT_LED_MAX_STALE_SEC + 5.0) * 1000.0
        existing = {
            "last": 0.12, "lastPrice": 0.12, "vol": 1210, "volume": 1210,
            "oi": 54321, "openInt": 54321, "bid1": 0.119, "ask1": 0.121,
            "fromTdx": True, "qtyFromQmt": True,
            "lastSource": "qmt", "qtySource": "qmt", "oiSource": "qmt",
            "bookSource": "tdx", "time": oldMs, "lastTime": oldMs,
            "qtyTime": oldMs, "oiTime": oldMs, "bookTime": oldMs,
            "lastChangedTime": oldMs, "qtyChangedTime": oldMs,
        }
        incoming = {
            "last": 0.12, "lastPrice": 0.12, "vol": 1210, "volume": 1210,
            "bid1": 0.119, "ask1": 0.121, "time": nowMs, "undl": "510300",
        }
        with m._cacheLock:
            m.optionTicks[code] = dict(existing)
        try:
            self.assertGreaterEqual(m.mergeTdxOptionTicks({code: incoming}), 1)
            got = m.optionTicks[code]
            self.assertEqual(got["lastSource"], "tdx")
            self.assertEqual(got["qtySource"], "tdx")
            self.assertGreater(got["lastTime"], oldMs)
            self.assertGreater(got["qtyTime"], oldMs)
            self.assertEqual(got["lastChangedTime"], oldMs)
            self.assertEqual(got["qtyChangedTime"], oldMs)
            self.assertEqual(got["oi"], existing["oi"])
            self.assertEqual(got["oiSource"], "qmt")
            self.assertEqual(got["oiTime"], oldMs)
        finally:
            with m._cacheLock:
                m.optionTicks.pop(code, None)

    def testTdxAllowedWhenFreshBookMasksStaleQmtLastAndVolume(self):
        """TDX 刷新的整票墙钟不能掩盖 QMT 字段自身已经超时。"""
        m = self.mod
        nowMs = datetime.now().timestamp() * 1000.0
        oldMs = nowMs - (m.QMT_LED_MAX_STALE_SEC + 5.0) * 1000.0
        existing = {
            "last": 0.12, "vol": 1210, "oi": 54321,
            "fromTdx": True, "qtyFromQmt": True,
            "lastSource": "qmt", "qtySource": "qmt", "oiSource": "qmt",
            "bookSource": "tdx", "time": nowMs, "bookTime": nowMs,
            "lastTime": oldMs, "qtyTime": oldMs, "oiTime": oldMs,
        }
        incoming = {"undl": "510300", "last": 0.12, "vol": 1210,
                    "bid1": 0.119, "ask1": 0.121, "time": nowMs}
        self.assertTrue(
            m.tdxOptCodeAllowed("10010974.SHO", incoming, None, existing),
            "最新/成交量仍是陈旧 QMT 时，TDX 必须获准接管，即使盘口墙钟刚刷新",
        )

    def testDirectQmtTicksStampFieldSources(self):
        """纯 QMT 行情须逐字段显式标源，不能靠页面端猜测。"""
        m = self.mod
        code = "10019991.SHO"
        nowMs = datetime.now().timestamp() * 1000.0
        with m._cacheLock:
            m.optionTicks.pop(code, None)
        try:
            m._applyOptionTicks(
                {
                    code: {
                        "last": 0.23,
                        "vol": 123,
                        "oi": 456,
                        "bid1": 0.229,
                        "ask1": 0.231,
                        "time": nowMs,
                    }
                },
                [],
            )
            got = m.optionTicks[code]
            self.assertEqual(got["lastSource"], "qmt")
            self.assertEqual(got["qtySource"], "qmt")
            self.assertEqual(got["oiSource"], "qmt")
            self.assertEqual(got["bookSource"], "qmt")
        finally:
            with m._cacheLock:
                m.optionTicks.pop(code, None)


class TdxOnlyFallbackTest(unittest.TestCase):
    """QMT 桥接端口离线时，15580 仍须通过 15581 保持可用。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testForcedTdxMergeCoversNominalQmtLedChain(self):
        from unittest.mock import patch

        m = self.mod
        code = "10019992.SHO"
        nowMs = datetime.now().timestamp() * 1000.0
        rec = {
            "undl": "510300",
            "last": 0.31,
            "vol": 10,
            "bid1": 0.30,
            "ask1": 0.32,
            "time": nowMs,
        }
        with m._cacheLock:
            m.optionTicks.pop(code, None)
        try:
            with patch.object(m, "isWatchdogWindow", return_value=True):
                self.assertFalse(m.tdxOptCodeAllowed(code, rec, None, None))
                self.assertGreaterEqual(
                    m.mergeTdxOptionTicks({code: rec}, forceTdx=True), 1
                )
            self.assertTrue(m.optionTicks[code].get("fromTdx"))
        finally:
            with m._cacheLock:
                m.optionTicks.pop(code, None)

    def testDedicatedTdxMergeIsDefaultAndInlineMergeIsDisabled(self):
        m = self.mod
        self.assertTrue(m.TDX_OPT_DEDICATED_MERGE)
        self.assertFalse(m.shouldMergeTdxInOptionPoll())

    def testDedicatedTdxMergeLoopDoesNotNeedQmtRpc(self):
        m = self.mod
        import logging
        import threading

        stop = threading.Event()
        calls = []

        def pollOnce(_logger=None, forceTdx=False):
            calls.append(forceTdx)
            if len(calls) >= 3:
                stop.set()
            return 1

        m.running = True
        try:
            m._runTdxMergeLoop(
                logging.getLogger("tdx-dedicated-test"),
                stop,
                pollFn=pollOnce,
                intervalSec=0.0,
            )
        finally:
            m.running = True
        self.assertEqual(calls, [False, False, False])

    def testPrepareTdxFallbackLoadsAllFiveChainsFromOptBasic(self):
        from unittest.mock import patch

        m = self.mod
        oldCodes = list(m._optionChainCodes)
        fallbackCodes = ["10010001.SHO", "90007001.SZO"]
        try:
            with (
                patch.object(m, "loadEtfOptionQmtCodes", return_value=fallbackCodes) as loadCodes,
                patch.object(m, "seedOptionMetaFromOptBasic", return_value=2) as seedMeta,
            ):
                out = m.prepareTdxFallbackChain(None)
            self.assertEqual(out, fallbackCodes)
            loadCodes.assert_called_once_with(m.OPT_UNDERLYINGS)
            seedMeta.assert_called_once_with(None, underlyings=m.OPT_UNDERLYINGS)
            self.assertEqual(m._optionChainCodes, fallbackCodes)
        finally:
            m._setOptionChainCodes(oldCodes)

    def testTdxFallbackPollOnceDoesNotTouchQmtRpc(self):
        from unittest.mock import patch

        m = self.mod
        with (
            patch.object(m, "fetchTdxOptSnapshot", return_value={"90007001.SZO": {}}),
            patch.object(m, "mergeTdxOptionTicks", return_value=7) as merge,
        ):
            self.assertEqual(m.pollTdxFallbackOnce(None), 7)
        self.assertTrue(merge.call_args.kwargs["forceTdx"])

    def testMainStartsTdxFallbackBeforeBlockingForQmtRpc(self):
        """启动顺序是故障根因：TDX-only 必须在 waitUntilRpcPing 之前启动。"""
        source = inspect.getsource(self.mod._runQuoteService)
        self.assertLess(
            source.index("prepareTdxFallbackChain(logger)"),
            source.index("waitUntilRpcPing(logger, xt_trader)"),
        )
        self.assertIn("tdxFallbackStop.set()", source)


# 代码更新时间：2026-09-10 14:05 OPT_QTY 含 SZO
# 代码更新时间：2026-09-10 13:15 冻住TDX仍禁止QMT整票、逼FillQty
# 代码更新时间：2026-09-10 11:33 QMT纠偏量仓；忽略仅时间变化
# 代码更新时间：2026-09-10 11:26 MergeKeepQty 量取较大值防 TDX 回盖
# 代码更新时间：2026-09-10 11:08 OPT_QTY 全推死时仍补；失败不跳 rotate
# 代码更新时间：2026-09-10 10:56 默认不跳TDX链chunk；FillQty补量仓+last
# 代码更新时间：2026-09-10 10:51 TDX缺oi时保留/QMT补仓；指纹含oi
# 代码更新时间：2026-09-10 09:27 空TDX不落库/不锁fresh；TDX覆盖跳过OPT_CHUNK
# 代码更新时间：2026-09-09 18:50 TDX超时2s、fromTdx快照与合并补标记单测
# 代码更新时间：2026-08-27 10:51 启动延迟拉链；直播窗窄口 META；ProductType
# 代码更新时间：2026-08-27 11:06 订满后仍灌 SZO 占位 1d
# 代码更新时间：2026-08-27 14:37 有量日K 盖过冻结 tick
# 代码更新时间：2026-08-27 14:57 新挂有量也定期灌 1d；opt_basic list_date 查询
# 代码更新时间：2026-08-27 17:56 拉链重试/SZO预热/近3日新挂/META窗口测试
# 代码更新时间：2026-08-27 19:34 交易日灌库窗口；直播窗忽略 tick 缺口等待
# 代码更新时间：2026-08-28 06:57 LIVE=1s；直播窗 1d 先于新挂灌库/META
# 代码更新时间：2026-08-28 07:08 片 280；直播 META 8；IDLE 10s
# 代码更新时间：2026-08-28 10:06 假活跳过慢 ping；片 50；LIVE 10s；round rest
# 代码更新时间：2026-08-28 10:20 订阅先 SZO；健康检查不含 SHO 缺口
# 代码更新时间：2026-08-28 10:25 1d 订阅宇宙仅为 SZO
# 代码更新时间：2026-08-28 10:35 SZO 订阅周期改为 tick
# 代码更新时间：2026-08-28 11:04 订 tick 无效；盘中灌当日 SZO tick
# 代码更新时间：2026-08-28 11:20 1m 加总成交量；tick 宇宙仅沪市
# 代码更新时间：2026-08-28 13:45 订阅空转当日放弃；1m 按分钟柱过期
# 代码更新时间：2026-08-28 15:07 订阅宇宙只留沪市；健康按配额判定
# 代码更新时间：2026-08-28 16:00 本地 datadir 直读：格式/残片/尾截取/下游衔接
# 代码更新时间：2026-08-31 15:55 后盘 catch-up；10 倍权利金缩放单测
# 代码更新时间：2026-09-02 09:55 SZO/SHO DAT 4 位小数；虚值认沽 0.039 不再读成 0.39
# 代码更新时间：2026-09-03 07:21 启动校桥接 revision；OPT_HEALTH dead_push
# 代码更新时间：2026-09-03 11:39 直播窗补 optMeta / preferCodes 单测
# 代码更新时间：2026-09-07 10:18 通达信 159915/510050 合并与兜底
# 代码更新时间：2026-09-07 17:47 tick 失败退避与 WARNING 限频
# 代码更新时间：2026-09-15 18:50 盘后禁 QMT catchup，仅盘前允许启动基线
# 代码更新时间：2026-10-01 08:04 TDX启动顺序守卫定位到信号主线程委托的服务生命周期
# 代码更新时间：2026-10-04 17:42 在市合约数改为与库内独立计数对账
