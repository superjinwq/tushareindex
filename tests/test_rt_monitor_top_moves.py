# coding: utf-8
import unittest

from rt_monitor.monolith import (
    TOP_MOVES_IDLE_PAUSE_SEC,
    TOP_MOVES_REFRESH_MIN_SEC,
    _topMovesRefreshPlan,
)


class TopMovesRefreshPlanTest(unittest.TestCase):
    def testStartupWarmsOnce(self):
        should, interval = _topMovesRefreshPlan(0.0, 100.0, False, False)
        self.assertTrue(should)
        self.assertEqual(interval, TOP_MOVES_REFRESH_MIN_SEC)

    def testStartupAfterWarmGoesIdle(self):
        should, interval = _topMovesRefreshPlan(0.0, 100.0, True, False)
        self.assertFalse(should)
        self.assertEqual(interval, TOP_MOVES_IDLE_PAUSE_SEC)

    def testWatchingFetches(self):
        should, _interval = _topMovesRefreshPlan(90.0, 100.0, True, False)
        self.assertTrue(should)

    def testIdleStopsFetch(self):
        should, interval = _topMovesRefreshPlan(80.0, 100.0, True, False)
        self.assertFalse(should)
        self.assertEqual(interval, TOP_MOVES_IDLE_PAUSE_SEC)

    def testHeavyYieldsWhileWatching(self):
        should, interval = _topMovesRefreshPlan(99.0, 100.0, True, True)
        self.assertFalse(should)
        self.assertEqual(interval, TOP_MOVES_REFRESH_MIN_SEC)


if __name__ == "__main__":
    unittest.main()
