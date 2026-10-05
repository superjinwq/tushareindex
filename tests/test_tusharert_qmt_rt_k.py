# coding: utf-8
import importlib.util
import os
import time
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tusharert个股实时行情.py")


def _loadMod():
    spec = importlib.util.spec_from_file_location("tusharert_rt_k", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class QmtRtKMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _loadMod()

    def testAShareIncludes302AndBj(self):
        self.assertTrue(self.mod.isAShareTsCode("000001.SZ"))
        self.assertTrue(self.mod.isAShareTsCode("302132.SZ"))
        self.assertTrue(self.mod.isAShareTsCode("688981.SH"))
        self.assertTrue(self.mod.isAShareTsCode("920021.BJ"))
        self.assertTrue(self.mod.isAShareTsCode("830799.BJ"))

    def testAShareExcludesEtfBondB(self):
        self.assertFalse(self.mod.isAShareTsCode("159915.SZ"))
        self.assertFalse(self.mod.isAShareTsCode("510300.SH"))
        self.assertFalse(self.mod.isAShareTsCode("110059.SH"))
        self.assertFalse(self.mod.isAShareTsCode("200596.SZ"))
        self.assertFalse(self.mod.isAShareTsCode("10011516.SHO"))

    def testVolumeHandToShareAndBook(self):
        asof = datetime(2026, 9, 7, 13, 52, 33).date()
        tick = {
            "lastPrice": 11.68,
            "lastClose": 11.89,
            "open": 11.870000000000001,
            "high": 11.88,
            "low": 11.65,
            "volume": 894511,
            "amount": 1050265300.0,
            "time": 1788760353000,
            "askPrice": [11.69, 11.70],
            "askVol": [8611, 7943],
            "bidPrice": [11.68, 11.67],
            "bidVol": [5689, 2690],
        }
        row = self.mod.qmtTickToRtKRow("000001.SZ", tick, asof)
        self.assertIsNotNone(row)
        self.assertEqual(row["vol"], 89451100.0)
        self.assertEqual(row["ask_volume1"], 861100.0)
        self.assertEqual(row["bid_volume1"], 568900.0)
        self.assertEqual(row["close"], 11.68)
        self.assertEqual(row["pre_close"], 11.89)
        self.assertEqual(row["open"], 11.87)
        self.assertNotIn("name", row)
        self.assertNotIn("num", row)

    def testRejectNonTodayAndZeroLast(self):
        asof = datetime(2026, 9, 7).date()
        tick = {
            "lastPrice": 0,
            "volume": 10,
            "time": datetime(2026, 9, 7, 13, 0).timestamp() * 1000,
        }
        self.assertIsNone(self.mod.qmtTickToRtKRow("000001.SZ", tick, asof))
        tick2 = {
            "lastPrice": 10.0,
            "volume": 10,
            "time": datetime(2026, 9, 6, 13, 0).timestamp() * 1000,
        }
        self.assertIsNone(self.mod.qmtTickToRtKRow("000001.SZ", tick2, asof))
        tick3 = {
            "lastPrice": 15.88,
            "volume": 0,
            "time": datetime(2026, 9, 7, 0, 0).timestamp() * 1000,
        }
        self.assertIsNone(self.mod.qmtTickToRtKRow("301699.SZ", tick3, asof))

    def testRejectEtfEvenIfMapped(self):
        asof = datetime(2026, 9, 7).date()
        tick = {
            "lastPrice": 2.8,
            "volume": 100,
            "time": datetime(2026, 9, 7, 13, 0).timestamp() * 1000,
        }
        self.assertIsNone(self.mod.qmtTickToRtKRow("159915.SZ", tick, asof))

    def testCacheNewerBlocksTushare(self):
        newer = datetime(2026, 9, 7, 13, 52, 33)
        older = datetime(2026, 9, 7, 13, 51, 57)
        self.assertTrue(self.mod.isCacheNewerThanIncoming(newer, older))
        self.assertFalse(self.mod.isCacheNewerThanIncoming(older, newer))

    def testRememberAllowCodesAcceptsIndex(self):
        import pandas as pd
        idx = pd.Index(["000001.SZ", "159915.SZ", "920021.BJ"])
        with self.mod.qmtLock:
            self.mod.qmtState["allowSet"].clear()
        self.mod.rememberAShareAllowCodes(idx)
        with self.mod.qmtLock:
            allow = set(self.mod.qmtState["allowSet"])
        self.assertIn("000001.SZ", allow)
        self.assertIn("920021.BJ", allow)
        self.assertNotIn("159915.SZ", allow)

    def testSanityMedianBand(self):
        ok = [1.0] * 20
        self.assertTrue(self.mod.medianVolRatioOk(ok))
        bad = [0.01] * 20
        self.assertFalse(self.mod.medianVolRatioOk(bad))
        self.assertFalse(self.mod.medianVolRatioOk([1.0] * 5))

    def testFreqLimitMatchesServerAndDocs(self):
        self.assertTrue(self.mod.isTushareFreqLimited("抱歉，您每分钟最多访问该接口50次"))
        self.assertTrue(self.mod.isTushareFreqLimited("频率超限(50次/分钟)"))
        self.assertFalse(self.mod.isTushareFreqLimited("权限不足，需要至少2000积分"))
        self.assertFalse(self.mod.isTushareFreqLimited("网络超时"))

    def testMarkServerLimitedFillsEmptyWindow(self):
        mgr = self.mod.SimpleTimeManager(max_calls=50, window_size=60)
        mgr.record_call()
        waitSec = mgr.markServerLimited()
        self.assertGreaterEqual(waitSec, 59.0)
        self.assertFalse(mgr.can_make_call())

    def testAdaptiveIntervalWidensAndRecovers(self):
        mgr = self.mod.SimpleTimeManager(
            max_calls=50,
            window_size=60,
            baseInterval=1.2,
            maxInterval=1.5,
            intervalStep=0.05,
            recoverOk=3,
        )
        self.assertAlmostEqual(mgr.currentInterval(), 1.2)
        self.assertAlmostEqual(mgr.widenOnLimit(), 1.25)
        self.assertAlmostEqual(mgr.widenOnLimit(), 1.30)
        for _ in range(20):
            mgr.widenOnLimit()
        self.assertAlmostEqual(mgr.currentInterval(), 1.5)
        mgr.noteCallOk()
        mgr.noteCallOk()
        self.assertAlmostEqual(mgr.currentInterval(), 1.5)
        self.assertAlmostEqual(mgr.noteCallOk(), 1.45)
        mgr.noteCallOk()
        mgr.noteCallOk()
        self.assertAlmostEqual(mgr.noteCallOk(), 1.40)

    def testLockPathIsFixedNotCwd(self):
        lockPath = self.mod.defaultTusharertLockPath()
        self.assertTrue(os.path.isabs(lockPath))
        cwd = os.path.abspath(os.getcwd())
        self.assertFalse(
            os.path.normcase(lockPath).startswith(os.path.normcase(cwd) + os.sep)
        )
        localApp = os.environ.get("LOCALAPPDATA")
        if localApp:
            self.assertTrue(
                os.path.normcase(lockPath).startswith(os.path.normcase(localApp))
            )
        self.assertEqual(self.mod.INSTANCE_MUTEX_NAME, r"Local\tusharert_rt_k")

    def testIsTusharertScriptCmd(self):
        self.assertTrue(
            self.mod.isTusharertScriptCmd(
                [r"C:\Python312\python.exe", "-u", r"D:\x\tusharert个股实时行情.py"]
            )
        )
        self.assertFalse(
            self.mod.isTusharertScriptCmd(
                ["python.exe", "-m", "unittest", "tests.test_tusharert_qmt_rt_k"]
            )
        )
        self.assertFalse(self.mod.isTusharertScriptCmd(["python.exe", "-c", "print(1)"]))

    def testWaitUntilApiSlotProceedsWhenFree(self):
        mgr = self.mod.SimpleTimeManager(max_calls=50, window_size=60)
        self.assertTrue(self.mod._waitUntilApiSlot(mgr, "rt_k"))

    def testWaitUntilApiSlotStopsWhenNotRunning(self):
        mgr = self.mod.SimpleTimeManager(max_calls=1, window_size=60)
        mgr.record_call()
        prev = self.mod.running
        try:
            self.mod.running = False
            self.assertFalse(self.mod._waitUntilApiSlot(mgr, "rt_k"))
        finally:
            self.mod.running = prev

    def testWaitUntilApiSlotDoesNotBlockFullWindow(self):
        mgr = self.mod.SimpleTimeManager(max_calls=50, window_size=60)
        mgr.markServerLimited()
        t0 = time.time()
        ok = self.mod._waitUntilApiSlot(mgr, "rt_k")
        elapsed = time.time() - t0
        self.assertFalse(ok)
        self.assertLess(elapsed, 2.5)
        self.assertGreaterEqual(elapsed, 0.45)


if __name__ == "__main__":
    unittest.main()
