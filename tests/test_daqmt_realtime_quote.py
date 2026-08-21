# coding: utf-8
import importlib.util
import os
import unittest
from datetime import datetime

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
        self.assertEqual(
            self.mod.decideWatchdogAction(True, 14.9, 0),
            "idle",
        )

    def testSilenceAtThresholdCheckPing(self):
        self.assertEqual(
            self.mod.decideWatchdogAction(True, 15.0, 0),
            "check_ping",
        )

    def testOutsideWindowIdleEvenIfSilent(self):
        self.assertEqual(
            self.mod.decideWatchdogAction(False, 60.0, 0),
            "idle",
        )

    def testGiveUpAfterThreeFails(self):
        self.assertEqual(
            self.mod.decideWatchdogAction(True, 30.0, 3),
            "give_up",
        )

    def testPingFailBlocksSubscribe(self):
        self.assertFalse(self.mod.canSubscribeAfterUnsubscribe(False, True))

    def testUnsubFailBlocksSubscribe(self):
        self.assertFalse(self.mod.canSubscribeAfterUnsubscribe(True, False))

    def testPingAndUnsubOkAllowsSubscribe(self):
        self.assertTrue(self.mod.canSubscribeAfterUnsubscribe(True, True))


class ScriptSafetyTest(unittest.TestCase):
    def testNoMysqlOrFullTickPrime(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("mysql", text.lower())
        self.assertNotIn("get_full_tick", text)
        self.assertNotIn("prime=True", text)
        self.assertIn("prime=False", text)
        self.assertNotIn("to_sql", text)
        self.assertNotIn("INSERT INTO", text)


if __name__ == "__main__":
    unittest.main()
