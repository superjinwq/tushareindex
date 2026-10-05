# coding: utf-8
import os
import time
import unittest
from unittest.mock import patch

from rt_monitor.services import opt_t_board as m


def _payload(tag: str) -> dict:
    return {
        'error': None,
        'sections': [{'tag': tag}],
        'row_count': 1,
        'quote_source': 'opt_daily',
        'quote_live_n': 0,
    }


def _live_snapshot(
    *,
    opt_wall: str,
    etf_wall: str,
    target_last: float = 0.1234,
    target_spot: float = 4.321,
    other_last: float = 0.5678,
    tick_time: int = 1789023600000,
) -> dict:
    return {
        'optWall': opt_wall,
        'etfWall': etf_wall,
        'opt': {
            '90000001.SZO': {
                'last': target_last,
                'vol': 100,
                'oi': 200,
                'bid1': target_last - 0.0001,
                'ask1': target_last + 0.0001,
                'fromTdx': True,
                'time': tick_time,
            },
            '10000001.SHO': {
                'last': other_last,
                'vol': 300,
                'oi': 400,
                'bid1': other_last - 0.0001,
                'ask1': other_last + 0.0001,
                'fromTdx': True,
                'time': tick_time,
            },
        },
        'optMeta': {
            '90000001.SZO': {
                'undl': '159915.SZ',
                'k': 4.3,
                'exp': '2026-09-23',
                'sm': '202609',
                'cp': 'C',
                'unit': 10000,
            },
            '10000001.SHO': {
                'undl': '510050.SH',
                'k': 3.0,
                'exp': '2026-09-23',
                'sm': '202609',
                'cp': 'P',
                'unit': 10000,
            },
        },
        'etf': {
            '159915.SZ': {
                'last': target_spot,
                'vol': 10000,
                'open': 4.3,
                'high': 4.4,
                'low': 4.2,
                'time': tick_time,
            },
            '510050.SH': {
                'last': 3.016,
                'vol': 20000,
                'open': 3.0,
                'high': 3.1,
                'low': 2.9,
                'time': tick_time,
            },
        },
    }


class OptTBoardCacheSwrTest(unittest.TestCase):
    def setUp(self):
        with m._tBoardCacheLock:
            m._tBoardCache.clear()
            m._tBoardRefreshing.clear()
        # 本类断言的是「内容敏感」的缓存键语义；2026-09-11 起默认启用
        # 复用桶（OPT_T_BOARD_REUSE_BUCKET_SEC=10）以打破重算反馈循环，
        # 桶内键不再随内容变化。这里关桶以保持原语义可测；
        # 桶语义另有 OptTBoardReuseBucketTest 覆盖。
        self._bucket = m._T_BOARD_REUSE_BUCKET_SEC
        m._T_BOARD_REUSE_BUCKET_SEC = 0.0

    def tearDown(self):
        m._T_BOARD_REUSE_BUCKET_SEC = self._bucket
        with m._tBoardCacheLock:
            m._tBoardCache.clear()
            m._tBoardRefreshing.clear()

    def testDefaultCodesAreFiveEtfsWhenEnvEmpty(self):
        env = {k: v for k, v in os.environ.items() if k != 'OPT_T_BOARD_DEFAULT_CODES'}
        with patch.dict(os.environ, env, clear=True):
            codes = m._opt_t_board_default_codes()
        self.assertEqual(codes, ['588000', '159915', '510050', '510300', '510500'])

    def testFreshHitDoesNotRecompute(self):
        calls: list[str] = []

        def fakeQuery(u, snapshot=m._SNAP_UNSET):
            calls.append(u)
            return _payload('fresh')

        with m._tBoardCacheLock:
            m._tBoardCache['588000'] = (time.monotonic(), _payload('cached'), 90.0)
        with (
            patch.object(m, '_query_opt_t_board_payload', side_effect=fakeQuery),
            patch.object(m, 'fetchDaqmtQuoteSnapshot', return_value=None),
        ):
            out = m.queryOptTBoardPayloadCached('588000')
        self.assertEqual(out['sections'][0]['tag'], 'cached')
        self.assertEqual(calls, [])

    def testStaleHitReturnsOldPayloadAndRefreshes(self):
        gate = {'n': 0}

        def fakeQuery(u, snapshot=m._SNAP_UNSET):
            gate['n'] += 1
            return _payload('refreshed')

        with m._tBoardCacheLock:
            m._tBoardCache['510300'] = (
                time.monotonic() - 10.0,
                _payload('stale'),
                3.0,
            )
        with (
            patch.object(m, '_query_opt_t_board_payload', side_effect=fakeQuery),
            patch.object(m, 'fetchDaqmtQuoteSnapshot', return_value=None),
        ):
            out = m.queryOptTBoardPayloadCached('510300')
            self.assertEqual(out['sections'][0]['tag'], 'stale')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._tBoardCacheLock:
                    hit = m._tBoardCache.get('510300')
                if hit is not None and hit[1]['sections'][0]['tag'] == 'refreshed':
                    refreshed = hit
                    break
                time.sleep(0.02)
            self.assertIsNotNone(refreshed)
            self.assertGreaterEqual(gate['n'], 1)

    def testPeekReturnsCachedPayload(self):
        with m._tBoardCacheLock:
            m._tBoardCache['159915'] = (time.monotonic(), _payload('peek'), 3.0)
        peeked = m.peekOptTBoardPayload('159915')
        self.assertEqual(peeked['sections'][0]['tag'], 'peek')
        self.assertIsNone(m.peekOptTBoardPayload('510050'))

    def testNeedsWarmOnlyWhenPeekMissing(self):
        with m._tBoardCacheLock:
            for u in m._optTBoardWarmCodeList('588000'):
                m._tBoardCache[u] = (time.monotonic(), _payload('w'), 90.0)
        self.assertFalse(m._optTBoardNeedsWarm('588000'))
        with m._tBoardCacheLock:
            m._tBoardCache.pop('510050', None)
        self.assertTrue(m._optTBoardNeedsWarm('588000'))

    def testHtmlFragReusesPayloadCache(self):
        payload = {
            'error': None,
            'sections': [],
            'row_count': 0,
        }
        calls = {'n': 0}

        def fakeRender(*args, **kwargs):
            calls['n'] += 1
            return '<table></table>'

        with patch.object(m, 'render_template', side_effect=fakeRender):
            a = m._opt_t_board_render_body_fragment(payload)
            b = m._opt_t_board_render_body_fragment(payload)
        self.assertEqual(a, b)
        self.assertEqual(calls['n'], 1)
        self.assertEqual(payload.get('_htmlFrag'), '<table></table>')
        self.assertTrue(m.optTBoardNotModified(payload.get('_htmlEtag'), payload.get('_htmlEtag')))

    def testNotModifiedIgnoresQuotes(self):
        self.assertTrue(m.optTBoardNotModified('"abc"', 'abc'))
        self.assertFalse(m.optTBoardNotModified('abc', 'def'))
        self.assertFalse(m.optTBoardNotModified('', 'abc'))

    def testLivePollMsMatchesTtl(self):
        self.assertEqual(m.optQuoteLivePollMs('daqmt'), 800)
        self.assertEqual(m.optQuoteLivePollMs('tdx'), 800)
        self.assertEqual(
            m.optQuoteLivePollMs('daqmt'),
            int(round(m._T_BOARD_LIVE_CACHE_TTL_SEC * 1000)),
        )
        self.assertEqual(m.optQuoteLivePollMs('opt_daily'), 10000)
        self.assertEqual(m.optQuoteLivePollMs(''), 10000)
        self.assertEqual(m._T_BOARD_LIVE_CACHE_TTL_SEC, 0.8)

    def testWallKeyChangeReturnsStaleAndRefreshes(self):
        """桶/墙键变了必须秒返旧值，后台刷新；禁止同步堵 2~5s。"""
        calls: list[str] = []

        def fakeQuery(u, snapshot=m._SNAP_UNSET):
            calls.append(u)
            return _payload('new-wall')

        oldSnap = _live_snapshot(opt_wall='w1', etf_wall='e1')
        newSnap = _live_snapshot(
            opt_wall='w2',
            etf_wall='e2',
            other_last=0.6789,
        )
        old = _payload('old-wall')
        old['_snapWallKey'] = m._daqmtSnapWallKey(oldSnap, '510050')
        with m._tBoardCacheLock:
            m._tBoardCache['510050'] = (time.monotonic(), old, 90.0)
        with (
            patch.object(m, '_query_opt_t_board_payload', side_effect=fakeQuery),
            patch.object(m, 'fetchDaqmtQuoteSnapshot', return_value=newSnap),
        ):
            out = m.queryOptTBoardPayloadCached('510050')
            self.assertEqual(out['sections'][0]['tag'], 'old-wall')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._tBoardCacheLock:
                    hit = m._tBoardCache.get('510050')
                if hit is not None and hit[1]['sections'][0]['tag'] == 'new-wall':
                    refreshed = hit
                    break
                time.sleep(0.02)
            self.assertIsNotNone(refreshed)
            self.assertEqual(
                refreshed[1].get('_snapWallKey'),
                m._daqmtSnapWallKey(newSnap, '510050'),
            )
            self.assertGreaterEqual(len(calls), 1)

    def testSameWallHitSkipsRecompute(self):
        calls: list[str] = []

        def fakeQuery(u, snapshot=m._SNAP_UNSET):
            calls.append(u)
            return _payload('should-not')

        snap = {'optWall': 'w1', 'etfWall': 'e1', 'opt': {}, 'etf': {}}
        cached = _payload('same-wall')
        cached['_snapWallKey'] = m._daqmtSnapWallKey(snap, '510050')
        with m._tBoardCacheLock:
            m._tBoardCache['510050'] = (time.monotonic(), cached, 0.8)
        with (
            patch.object(m, '_query_opt_t_board_payload', side_effect=fakeQuery),
            patch.object(m, 'fetchDaqmtQuoteSnapshot', return_value=snap),
        ):
            out = m.queryOptTBoardPayloadCached('510050')
        self.assertEqual(out['sections'][0]['tag'], 'same-wall')
        self.assertEqual(calls, [])

    def testUnrelatedSnapshotChangeKeepsTargetCache(self):
        calls: list[str] = []

        def fakeQuery(u, snapshot=m._SNAP_UNSET):
            calls.append(u)
            return _payload('should-not-recompute')

        oldSnap = _live_snapshot(opt_wall='w1', etf_wall='e1')
        newSnap = _live_snapshot(
            opt_wall='w2',
            etf_wall='e2',
            other_last=0.6789,
            tick_time=1789023605000,
        )
        cached = _payload('target-unchanged')
        cached['_snapWallKey'] = m._daqmtSnapWallKey(oldSnap, '159915')
        with m._tBoardCacheLock:
            m._tBoardCache['159915'] = (
                time.monotonic() - 2.0,
                cached,
                m._T_BOARD_LIVE_CACHE_TTL_SEC,
            )
        with (
            patch.object(m, '_query_opt_t_board_payload', side_effect=fakeQuery),
            patch.object(m, 'fetchDaqmtQuoteSnapshot', return_value=newSnap),
        ):
            out = m.queryOptTBoardPayloadCached('159915')
        self.assertEqual(out['sections'][0]['tag'], 'target-unchanged')
        self.assertEqual(calls, [])

    def testTargetQuoteChangeReturnsStaleAndRefreshes(self):
        calls: list[str] = []

        def fakeQuery(u, snapshot=m._SNAP_UNSET):
            calls.append(u)
            return _payload('target-changed')

        oldSnap = _live_snapshot(opt_wall='w1', etf_wall='e1')
        newSnap = _live_snapshot(
            opt_wall='w2',
            etf_wall='e2',
            target_last=0.1334,
        )
        cached = _payload('old-target')
        cached['_snapWallKey'] = m._daqmtSnapWallKey(oldSnap, '159915')
        with m._tBoardCacheLock:
            m._tBoardCache['159915'] = (
                time.monotonic(),
                cached,
                m._T_BOARD_LIVE_CACHE_TTL_SEC,
            )
        with (
            patch.object(m, '_query_opt_t_board_payload', side_effect=fakeQuery),
            patch.object(m, 'fetchDaqmtQuoteSnapshot', return_value=newSnap),
        ):
            out = m.queryOptTBoardPayloadCached('159915')
            self.assertEqual(out['sections'][0]['tag'], 'old-target')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._tBoardCacheLock:
                    hit = m._tBoardCache.get('159915')
                if hit is not None and hit[1]['sections'][0]['tag'] == 'target-changed':
                    refreshed = hit
                    break
                time.sleep(0.02)
            self.assertIsNotNone(refreshed)
            self.assertEqual(
                refreshed[1].get('_snapWallKey'),
                m._daqmtSnapWallKey(newSnap, '159915'),
            )
            self.assertGreaterEqual(len(calls), 1)

    def testTargetEtfChangeReturnsStaleAndRefreshes(self):
        calls: list[str] = []

        def fakeQuery(u, snapshot=m._SNAP_UNSET):
            calls.append(u)
            return _payload('target-etf-changed')

        oldSnap = _live_snapshot(opt_wall='w1', etf_wall='e1')
        newSnap = _live_snapshot(
            opt_wall='w2',
            etf_wall='e2',
            target_spot=4.322,
        )
        cached = _payload('old-target-etf')
        cached['_snapWallKey'] = m._daqmtSnapWallKey(oldSnap, '159915')
        with m._tBoardCacheLock:
            m._tBoardCache['159915'] = (
                time.monotonic(),
                cached,
                m._T_BOARD_LIVE_CACHE_TTL_SEC,
            )
        with (
            patch.object(m, '_query_opt_t_board_payload', side_effect=fakeQuery),
            patch.object(m, 'fetchDaqmtQuoteSnapshot', return_value=newSnap),
        ):
            out = m.queryOptTBoardPayloadCached('159915')
            self.assertEqual(out['sections'][0]['tag'], 'old-target-etf')
            refreshed = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with m._tBoardCacheLock:
                    hit = m._tBoardCache.get('159915')
                if (
                    hit is not None
                    and hit[1]['sections'][0]['tag'] == 'target-etf-changed'
                ):
                    refreshed = hit
                    break
                time.sleep(0.02)
            self.assertIsNotNone(refreshed)
            self.assertGreaterEqual(len(calls), 1)

    def testTargetMetaUnitChangeChangesContentKey(self):
        oldSnap = _live_snapshot(opt_wall='w1', etf_wall='e1')
        newSnap = _live_snapshot(opt_wall='w2', etf_wall='e2')
        newSnap['optMeta']['90000001.SZO']['unit'] = 5000
        self.assertNotEqual(
            m._daqmtSnapWallKey(oldSnap, '159915'),
            m._daqmtSnapWallKey(newSnap, '159915'),
        )

    def testMissingShoMetaTickChangeInvalidatesConservatively(self):
        oldSnap = _live_snapshot(opt_wall='w1', etf_wall='e1')
        newSnap = _live_snapshot(
            opt_wall='w2',
            etf_wall='e2',
            other_last=0.6789,
        )
        oldSnap['optMeta'].pop('10000001.SHO')
        newSnap['optMeta'].pop('10000001.SHO')
        self.assertNotEqual(
            m._daqmtSnapWallKey(oldSnap, '510050'),
            m._daqmtSnapWallKey(newSnap, '510050'),
        )

    def testFrontendPollAlignedToTtl(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(root, 'templates', 'rt_monitor_opt_t_board.html'),
            encoding='utf-8',
        ) as fh:
            html = fh.read()
        with open(
            os.path.join(root, 'static', 'js', 'rt_monitor_opt_sp_plan.js'),
            encoding='utf-8',
        ) as fh:
            js = fh.read()
        self.assertNotIn("source === 'daqmt' ? 3000", html)
        self.assertNotIn("source === 'daqmt' ? 8000", html)
        self.assertNotIn("source === 'daqmt' ? 4000", html)
        self.assertNotIn("source === 'daqmt' ? 2000", html)
        # 整表已从 800ms 改为约 5 秒；盘口/量仓另走 1 秒 delta。
        self.assertNotIn("return 800;", html)
        self.assertIn("OPT_T_HEAVY_REFRESH_MS = 5000", html)
        self.assertIn("OPT_T_DELTA_REFRESH_MS = 1000", html)
        self.assertIn('X-Opt-Poll-Ms', html)
        self.assertIn('poll_ms', html)
        self.assertIn('setInterval(loadPreview, 4000)', js)
        self.assertNotIn('setInterval(loadPreview, 8000)', js)
        self.assertNotIn('setInterval(loadPreview, 5000)', js)


class OptTBoardReuseBucketTest(unittest.TestCase):
    """复用桶语义（2026-09-11）：桶内键稳定、跨桶必变、关桶退回内容敏感。

    动机：逐笔内容每 0.5~5s 真变，若键随内容变则缓存永不命中，每个请求都
    同步全量重算，API 被压到 3~5s（单次重算仅 ~0.5s）。
    """

    def setUp(self):
        self._bucket = m._T_BOARD_REUSE_BUCKET_SEC

    def tearDown(self):
        m._T_BOARD_REUSE_BUCKET_SEC = self._bucket

    @staticmethod
    def _snap(spot: float) -> dict:
        return {
            'optWall': '2026-09-11T11:00:00',
            'etf': {'510050.SH': {'last': spot}},
            'optMeta': {'10000001.SHO': {'undl': '510050.SH'}},
            'opt': {'10000001.SHO': {'last': 0.1, 'volume': 5}},
        }

    def testBucketOnKeyIgnoresContentChange(self):
        m._T_BOARD_REUSE_BUCKET_SEC = 3600.0  # 长桶，避免跨桶抖动
        k1 = m._daqmtSnapWallKey(self._snap(2.80), '510050')
        k2 = m._daqmtSnapWallKey(self._snap(9.99), '510050')
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith('b'))

    def testBucketOffKeyIsContentSensitive(self):
        m._T_BOARD_REUSE_BUCKET_SEC = 0.0
        k1 = m._daqmtSnapWallKey(self._snap(2.80), '510050')
        k2 = m._daqmtSnapWallKey(self._snap(9.99), '510050')
        self.assertNotEqual(k1, k2)
        self.assertFalse(k1.startswith('b'))

    def testBucketBoundaryChangesKey(self):
        """跨桶必须换键，保证新桶首个请求会重算拿到最新数据。"""
        m._T_BOARD_REUSE_BUCKET_SEC = 1.0
        with patch.object(m.time, 'time', return_value=1000.0):
            k1 = m._daqmtSnapWallKey(self._snap(2.80), '510050')
        with patch.object(m.time, 'time', return_value=1001.5):
            k2 = m._daqmtSnapWallKey(self._snap(2.80), '510050')
        self.assertNotEqual(k1, k2)


class OptTBoardAgeColoringPhaseTest(unittest.TestCase):
    """灰阶陈旧度只在连续竞价时段生效（2026-09-11 用户反馈）。

    午休/盘前/盘后行情本就冻结，按 >10s/>60s 着色会把整屏刷灰，属噪声。
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = m

    def testActiveOnlyInContinuousAuction(self):
        from datetime import datetime

        m = self.mod
        on = ('09:15', '09:20', '10:00', '11:29', '13:00', '14:59')
        off = ('00:00', '09:14', '11:30', '12:00', '12:59', '15:00', '16:00', '23:00')
        for hm in on:
            hh, mm = (int(x) for x in hm.split(':'))
            self.assertTrue(
                m.optTAgeColoringActive(datetime(2026, 9, 11, hh, mm)), hm
            )
        for hm in off:
            hh, mm = (int(x) for x in hm.split(':'))
            self.assertFalse(
                m.optTAgeColoringActive(datetime(2026, 9, 11, hh, mm)), hm
            )

    def testForceFreshBlanksAllFields(self):
        m = self.mod
        out = m._optTQuoteFieldTimes(None, 1.0, forceFresh=True)
        self.assertEqual(
            out, {'last': '', 'vol': '', 'oi': '', 'book': ''}
        )

    def testAgeClassThresholds(self):
        """阈值语义：<30s 正常 / 30~300s 淡灰 / >300s 深灰 / 无时钟最深灰。

        阈值标定依据见 opt_t_board._OPT_T_AGE_* 注释与
        scripts/tune_age_thresholds.py（系统投递中位 8.5s、P75 23s）。
        """
        m = self.mod
        now = 1_000_000_000_000.0
        self.assertEqual(m._OPT_T_AGE_YOUNG_SEC, 30.0)
        self.assertEqual(m._OPT_T_AGE_OLD_SEC, 300.0)
        self.assertEqual(m._optTAgeClass(now - 5_000, now), '')
        self.assertEqual(m._optTAgeClass(now - 29_000, now), '')
        self.assertEqual(m._optTAgeClass(now - 31_000, now), 'opt-t-age-stale')
        self.assertEqual(m._optTAgeClass(now - 299_000, now), 'opt-t-age-stale')
        self.assertEqual(m._optTAgeClass(now - 301_000, now), 'opt-t-age-ancient')
        self.assertEqual(m._optTAgeClass(0, now), 'opt-t-age-ancient')


    def testFrontendHasBackgroundRefreshForOtherChains(self):
        """非可见链需后台低频刷新（2026-09-11 用户确认方案）。

        规则：可见链 800ms（scheduleLiveRefresh），其余链 5~10s 低频
        （backgroundRefreshTick，轮转单条），避免五路 800ms 把后端 API 压回秒级。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import re as _re

        with open(
            os.path.join(root, 'templates', 'rt_monitor_opt_t_board.html'),
            encoding='utf-8',
        ) as fh:
            html = fh.read()
        self.assertIn('function backgroundRefreshTick', html)
        self.assertIn('function scheduleBackgroundRefresh', html)
        self.assertIn('scheduleBackgroundRefresh();', html)
        # 默认间隔在 5~10s 区间
        m = _re.search(
            r"__optBgRefreshSec,\s*10\);\s*if\s*\(!\(sec > 0\)\)\s*sec\s*=\s*(\d+)",
            html,
        )
        self.assertIsNotNone(m, '未找到后台刷新默认间隔')
        interval = int(m.group(1))
        self.assertGreaterEqual(interval, 5)
        self.assertLessEqual(interval, 10)
        # 可见链仍按 poll_ms 高频
        self.assertIn('loadUnderlying(getActiveUnderlying()', html)
        # 后台刷新不得触碰 DOM
        seg = html[html.index('function backgroundRefreshTick'):
                   html.index('function scheduleBackgroundRefresh')]
        self.assertNotIn('innerHTML', seg)

    def testFrontendSeparatesOneSecondQuoteDeltaFromHeavyBoardRefresh(self):
        """盘口/量仓不能再靠整表重算；IV/希腊值仍保留五秒级重算。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(root, 'templates', 'rt_monitor_opt_t_board.html'),
            encoding='utf-8',
        ) as fh:
            page = fh.read()
        with open(
            os.path.join(root, 'templates', 'rt_monitor_opt_t_board_body.html'),
            encoding='utf-8',
        ) as fh:
            body = fh.read()

        self.assertIn('function optTQuoteDeltaApiPath', page)
        self.assertIn('function loadQuoteDelta', page)
        self.assertIn('function scheduleQuoteDeltaRefresh', page)
        self.assertIn('OPT_T_HEAVY_REFRESH_MS', page)
        self.assertIn('data-opt-quote-code', body)
        self.assertIn('data-opt-quote-field', body)
        # 轻量刷新仅逐单元格写 textContent，不能整块 innerHTML 重绘。
        seg = page[page.index('function loadQuoteDelta'):
                   page.index('function scheduleQuoteDeltaRefresh')]
        self.assertNotIn('innerHTML', seg)


if __name__ == '__main__':
    unittest.main()

# 代码更新时间：2026-08-28 06:57 直播轮询 8s 对齐 TTL；前端去掉 3s
# 代码更新时间：2026-08-28 07:08 直播 4s / 盘后 10s
# 代码更新时间：2026-09-10 14:05 直播 TTL 0.8s；墙钟失效；前端 800ms
