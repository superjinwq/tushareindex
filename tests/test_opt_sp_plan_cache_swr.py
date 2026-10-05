# coding: utf-8
import time
import unittest
from unittest.mock import patch

from rt_monitor.services import opt_sp_plan as m


class OptSpPlanCacheSwrTest(unittest.TestCase):
    def setUp(self):
        with m._spPlanCacheLock:
            m._spPlanCache.clear()
            m._spPlanRefreshing.clear()
        with m._spPreviewCacheLock:
            m._spPreviewCache.clear()
            m._spPreviewRefreshing.clear()

    def tearDown(self):
        with m._spPlanCacheLock:
            m._spPlanCache.clear()
            m._spPlanRefreshing.clear()
        with m._spPreviewCacheLock:
            m._spPreviewCache.clear()
            m._spPreviewRefreshing.clear()

    def testDisabledSkipsCompute(self):
        calls: list[str] = []

        def fakeUncached(u, *, historyLimit=5, capitalWan=None):
            calls.append(u)
            return {'ok': True, 'enabled': True, 'u': u}

        with patch.object(m, '_querySpPlanUncached', side_effect=fakeUncached):
            out = m.querySpPlan('510300', historyLimit=5)
        self.assertTrue(out['ok'])
        self.assertFalse(out['enabled'])
        self.assertEqual(out['u'], '510300')
        self.assertEqual(calls, [])

    def testFreshHitDoesNotRecompute(self):
        calls: list[str] = []
        cached = {'ok': True, 'enabled': True, 'u': '588000', 'tag': 'cached'}
        wan = m.parseCapitalWan(m.CAPITAL_WAN_DEFAULT)
        with m._spPlanCacheLock:
            m._spPlanCache[('588000', 5, wan)] = (time.monotonic(), cached)
        with patch.object(m, '_querySpPlanUncached', side_effect=lambda *a, **k: calls.append('x') or cached):
            out = m.querySpPlan('588000', historyLimit=5)
        self.assertEqual(out['tag'], 'cached')
        self.assertEqual(calls, [])

    def testStaleHitReturnsOldAndRefreshes(self):
        wan = m.parseCapitalWan(m.CAPITAL_WAN_DEFAULT)
        stale = {'ok': True, 'enabled': True, 'u': '510500', 'tag': 'stale'}
        with m._spPlanCacheLock:
            m._spPlanCache[('510500', 5, wan)] = (
                time.monotonic() - m._SP_PLAN_CACHE_TTL_SEC - 1.0,
                stale,
            )

        def fakeUncached(u, *, historyLimit=5, capitalWan=None):
            return {'ok': True, 'enabled': True, 'u': u, 'tag': 'refreshed'}

        with patch.object(m, '_querySpPlanUncached', side_effect=fakeUncached):
            out = m.querySpPlan('510500', historyLimit=5)
            self.assertEqual(out['tag'], 'stale')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._spPlanCacheLock:
                    hit = m._spPlanCache.get(('510500', 5, wan))
                if hit is not None and hit[1].get('tag') == 'refreshed':
                    refreshed = hit
                    break
                time.sleep(0.02)
        self.assertIsNotNone(refreshed)

    def testEnabledCodesAreThreeUnderlyings(self):
        codes = m.spEnabledCodes()
        self.assertEqual(set(codes), {'588000', '510500', '159915'})

    def testPreviewFreshHitDoesNotRecompute(self):
        calls: list[str] = []
        cached = {'ok': True, 'enabled': True, 'u': '588000', 'tag': 'cached'}
        wan = m.parseCapitalWan(m.CAPITAL_WAN_DEFAULT)
        with m._spPreviewCacheLock:
            m._spPreviewCache[('588000', wan)] = (time.monotonic(), cached)
        with patch.object(m, '_querySpPreviewUncached', side_effect=lambda *a, **k: calls.append('x') or cached):
            out = m.querySpPreview('588000')
        self.assertEqual(out['tag'], 'cached')
        self.assertEqual(calls, [])

    def testPreviewStaleHitReturnsOldAndRefreshes(self):
        wan = m.parseCapitalWan(m.CAPITAL_WAN_DEFAULT)
        stale = {'ok': True, 'enabled': True, 'u': '510500', 'tag': 'stale'}
        with m._spPreviewCacheLock:
            m._spPreviewCache[('510500', wan)] = (
                time.monotonic() - m._SP_PREVIEW_CACHE_TTL_SEC - 1.0,
                stale,
            )

        def fakeUncached(u, *, capitalWan=None, snapshot=None, today=None):
            return {'ok': True, 'enabled': True, 'u': u, 'tag': 'refreshed'}

        with patch.object(m, '_querySpPreviewUncached', side_effect=fakeUncached):
            out = m.querySpPreview('510500')
            self.assertEqual(out['tag'], 'stale')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._spPreviewCacheLock:
                    hit = m._spPreviewCache.get(('510500', wan))
                if hit is not None and hit[1].get('tag') == 'refreshed':
                    refreshed = hit
                    break
                time.sleep(0.02)
        self.assertIsNotNone(refreshed)

    def testPreviewExplicitSnapshotBypassesCache(self):
        calls: list[str] = []
        wan = m.parseCapitalWan(m.CAPITAL_WAN_DEFAULT)
        cached = {'ok': True, 'enabled': True, 'u': '588000', 'tag': 'cached'}
        with m._spPreviewCacheLock:
            m._spPreviewCache[('588000', wan)] = (time.monotonic(), cached)

        def fakeUncached(u, *, capitalWan=None, snapshot=None, today=None):
            calls.append(u)
            return {'ok': True, 'enabled': True, 'u': u, 'tag': 'direct'}

        with patch.object(m, '_querySpPreviewUncached', side_effect=fakeUncached):
            out = m.querySpPreview('588000', snapshot={'opt': {}})
        self.assertEqual(out['tag'], 'direct')
        self.assertEqual(calls, ['588000'])

    def testPeekReturnsCacheWithoutCompute(self):
        calls: list[str] = []

        def fakeUncached(u, *, historyLimit=5, capitalWan=None):
            calls.append(u)
            return {'ok': True, 'enabled': True, 'u': u}

        with patch.object(m, '_querySpPlanUncached', side_effect=fakeUncached):
            self.assertIsNone(m.peekSpPlan('588000'))
        self.assertEqual(calls, [])
        payload = {'ok': True, 'enabled': True, 'u': '588000', 'tag': 'peek'}
        wan = m.parseCapitalWan(m.CAPITAL_WAN_DEFAULT)
        with m._spPlanCacheLock:
            m._spPlanCache[('588000', 5, wan)] = (time.monotonic(), payload)
        with patch.object(m, '_querySpPlanUncached', side_effect=fakeUncached):
            out = m.peekSpPlan('588000')
        self.assertEqual(out['tag'], 'peek')
        self.assertEqual(calls, [])
        disabled = m.peekSpPlan('510300')
        self.assertTrue(disabled['ok'])
        self.assertFalse(disabled['enabled'])


# 代码更新时间：2026-09-02 07:49
# 代码更新时间：2026-09-23 07:53 peekSpPlan 只读缓存、未接入直接 disabled
