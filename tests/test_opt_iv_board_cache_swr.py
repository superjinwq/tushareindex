# coding: utf-8
import time
import unittest
from unittest.mock import patch

from rt_monitor.services import opt_iv_board as m


def _payload(tag: str) -> dict:
    return {
        'error': None,
        'blocks': [{'tag': tag}],
        'asof': '2026-08-27',
        'rate_label': '',
        'quote_source': 'opt_iv_panel',
    }


class OptIvBoardCacheSwrTest(unittest.TestCase):
    def setUp(self):
        with m._ivBoardCacheLock:
            m._ivBoardDailyCache = None
            m._ivBoardLiveCache = None
            m._ivBoardDailyRefreshing = False
            m._ivBoardLiveRefreshing = False

    def tearDown(self):
        with m._ivBoardCacheLock:
            m._ivBoardDailyCache = None
            m._ivBoardLiveCache = None
            m._ivBoardDailyRefreshing = False
            m._ivBoardLiveRefreshing = False

    def testDailyFreshHitDoesNotRecompute(self):
        calls: list[str] = []
        with m._ivBoardCacheLock:
            m._ivBoardDailyCache = (time.monotonic(), _payload('cached'))
        with patch.object(m, '_queryDailyIvBoardUncached', side_effect=lambda: calls.append('x') or _payload('x')):
            out = m._getDailyIvBoard()
        self.assertEqual(out['blocks'][0]['tag'], 'cached')
        self.assertEqual(calls, [])

    def testDailyStaleHitReturnsOldAndRefreshes(self):
        with m._ivBoardCacheLock:
            m._ivBoardDailyCache = (
                time.monotonic() - m._IV_BOARD_CACHE_TTL_SEC - 1.0,
                _payload('stale'),
            )

        def fakeUncached():
            return _payload('refreshed')

        with patch.object(m, '_queryDailyIvBoardUncached', side_effect=fakeUncached):
            out = m._getDailyIvBoard()
            self.assertEqual(out['blocks'][0]['tag'], 'stale')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._ivBoardCacheLock:
                    hit = m._ivBoardDailyCache
                if hit is not None and hit[1]['blocks'][0]['tag'] == 'refreshed':
                    refreshed = hit
                    break
                time.sleep(0.02)
        self.assertIsNotNone(refreshed)

    def testLiveStaleHitReturnsOldAndRefreshes(self):
        with m._ivBoardCacheLock:
            m._ivBoardLiveCache = (
                time.monotonic() - m._IV_BOARD_LIVE_CACHE_TTL_SEC - 1.0,
                _payload('stale'),
            )

        def fakeCompute():
            data = _payload('refreshed')
            return m._storeLiveIvBoard(data)

        with patch.object(m, '_computeIvLivePayload', side_effect=fakeCompute):
            out = m.queryOptIvBoardPayload()
            self.assertEqual(out['blocks'][0]['tag'], 'stale')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._ivBoardCacheLock:
                    hit = m._ivBoardLiveCache
                if hit is not None and hit[1]['blocks'][0]['tag'] == 'refreshed':
                    refreshed = hit
                    break
                time.sleep(0.02)
        self.assertIsNotNone(refreshed)


if __name__ == '__main__':
    unittest.main()
