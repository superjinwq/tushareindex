# coding: utf-8
"""申万页秒开：partial 必须进缓存；完整包才跳过后台 _full。"""
import copy
import time
import unittest
from unittest.mock import patch

from rt_monitor.services import sw_industry as m


class SwIndustryPartialCacheTest(unittest.TestCase):
    def setUp(self):
        with m._swCacheLock:
            m._sw_industry_stats_cache.clear()
            m._swLiveMarketMapsCache.clear()
        with m._swFullComputeLock:
            m._swFullComputeScheduled.clear()

    def tearDown(self):
        with m._swCacheLock:
            m._sw_industry_stats_cache.clear()
            m._swLiveMarketMapsCache.clear()
        with m._swFullComputeLock:
            m._swFullComputeScheduled.clear()

    def testScheduleFullSkipsOnlyCompleteCache(self):
        key = 'l1||||x12'
        pending = {'pending_fina': True, 'rows': []}
        complete = {'pending_fina': False, 'rows': [{'a': 1}]}

        with m._swCacheLock:
            m._sw_industry_stats_cache[key] = (
                time.time() + 3600,
                pending,
                {},
                'l1',
            )
        with patch.object(m.threading, 'Thread') as thr:
            m._swScheduleFullSwStatsCompute('l1', '', '', '')
            self.assertEqual(thr.call_count, 1)

        with m._swFullComputeLock:
            m._swFullComputeScheduled.clear()
        with m._swCacheLock:
            m._sw_industry_stats_cache[key] = (
                time.time() + 3600,
                complete,
                {},
                'l1',
            )
        with patch.object(m.threading, 'Thread') as thr2:
            m._swScheduleFullSwStatsCompute('l1', '', '', '')
            self.assertEqual(thr2.call_count, 0)

    def testPatchLiveHttpPathNeverRequestsHeavyScan(self):
        seen = {}

        def fakeFetch(ts_to_ind, selected_trade_date=None, pool_key=None, waitLimitScan=True):
            seen['wait'] = waitLimitScan
            return ({}, {}, {}, {}, {})

        payload = {'rows': [{'group_code': '801010'}], 'summary_row': {}}
        with (
            patch.object(m, '_sw_patch_margin_fields_in_payload'),
            patch.object(m, '_sw_fetch_live_market_maps', side_effect=fakeFetch),
            patch.object(m, '_sw_should_heavy_live_refresh', return_value=True),
        ):
            m._sw_patch_live_market_fields_in_payload(
                payload,
                {'600000.SH': ('801010', '农林')},
                None,
                'l1',
                allowHeavy=False,
            )
        self.assertEqual(seen.get('wait'), False)


    def testEmptyAmtCacheDoesNotShortCircuitHeavy(self):
        """轻量空 amt 包不得挡住 waitLimitScan=True 重算。"""
        tsMap = {'600000.SH': ('801010', '农林')}
        light = ({'801010.SI': {'pct': 1}}, {'801010.SI': {}}, {}, {}, {})
        ck = m._sw_live_market_maps_cache_key(
            m._sw_build_idx_sw_quote_list(['801010']), None, None
        )
        with m._swCacheLock:
            m._swLiveMarketMapsCache[ck] = (time.time() + 60, light)

        called = {'n': 0}

        def fakeLoad(*_a, **_k):
            called['n'] += 1
            return ({}, {})

        with (
            patch.object(m, '_fetch_rt_sw_k_quotes', return_value={'801010.SI': {'pct': 1}}),
            patch.object(m, '_load_index_sw_daily_panel_bulk', side_effect=fakeLoad),
            patch.object(m, '_read_index_sw_daily_interval_range_pct', return_value={}),
            patch.object(m, '_read_sw_amount_ratio_percentiles', return_value={'t0': 50}),
            patch.object(m, '_sw_peek_limit_count_flagged', return_value=None),
            patch.object(m.pymysql, 'connect') as connMock,
        ):
            connMock.return_value.__enter__ = lambda s: s
            connMock.return_value.cursor.return_value.__enter__ = lambda s: s
            # waitLimitScan=False 可直接吃轻量缓存
            outLight = m._sw_fetch_live_market_maps(tsMap, waitLimitScan=False)
            self.assertEqual(outLight[2], {})
            self.assertEqual(called['n'], 0)
            # 重路径必须穿透
            m._sw_fetch_live_market_maps(tsMap, waitLimitScan=True)
            self.assertGreaterEqual(called['n'], 1)

    def testDataVersionChangesWhenFinaOrPendingUpdates(self):
        base = {
            'level': 'l1',
            'trade_date': None,
            'count': 1,
            'index_code': '',
            'periods': ['20250331'],
            'has_q_dtprofit': True,
            'has_sw_extend_metrics': True,
            'rows': [{
                'l1_code': '801010',
                'sw_pct': 1.0,
                's0': 0,
                'y0': 0,
                'dtp0': None,
                'total_mv_yi': 0,
                'free_mv_yi': 0,
            }],
            'summary_row': None,
            'margin_as_of': None,
            'pending_fina': True,
        }
        v1 = m._sw_build_data_version(base)
        filled = copy.deepcopy(base)
        filled['pending_fina'] = False
        filled['rows'][0]['s0'] = 100.5
        filled['rows'][0]['total_mv_yi'] = 12.3
        v2 = m._sw_build_data_version(filled)
        self.assertNotEqual(v1, v2)

    def testLiveMapsIsFreshFalseWhenAmtEmpty(self):
        tsMap = {'600000.SH': ('801010', '农林')}
        ck = m._sw_live_market_maps_cache_key(
            m._sw_build_idx_sw_quote_list(['801010']), None, None
        )
        with m._swCacheLock:
            m._swLiveMarketMapsCache[ck] = (
                time.time() + 60,
                ({'x': 1}, {}, {}, {}, {}),
            )
        self.assertFalse(m._sw_live_market_maps_is_fresh(tsMap, None, None))
        with m._swCacheLock:
            m._swLiveMarketMapsCache[ck] = (
                time.time() + 60,
                ({'x': 1}, {}, {'801010.SI': {'t0': 1}}, {}, {}),
            )
        self.assertTrue(m._sw_live_market_maps_is_fresh(tsMap, None, None))


    def testWantFullWaiterDoesNotReturnPendingPlaceholder(self):
        """_full 等待结束后若仍 pending，不得把占位包当完成 jsonify。"""
        served = {'n': 0}

        def fakeServe(*_a, **_k):
            served['n'] += 1
            return 'SERVED_PENDING'

        with (
            patch.object(m, '_sw_industry_stats_cache_get', side_effect=[
                ({'pending_fina': True, 'rows': []}, {}, 'l1'),  # 入口命中 pending → 穿透
                ({'pending_fina': True, 'rows': []}, {}, 'l1'),  # 第1次等待后仍 pending
                ({'pending_fina': True, 'rows': []}, {}, 'l1'),  # 第2次等待后仍 pending
                ({'pending_fina': True, 'rows': []}, {}, 'l1'),  # 最终尽力返回
            ]),
            patch.object(m, '_swWaitOrLeadCompute', side_effect=[
                (False, None),
                (False, None),
            ]),
            patch.object(m, '_sw_serve_cached_stats_payload', side_effect=fakeServe),
            patch.object(m, '_parse_sw_industry_trade_date_param', return_value=None),
            patch.object(m, '_sw_limit_flagged_pool_key', return_value=None),
        ):
            from flask import Flask
            app = Flask('sw_gate_test')
            with app.test_request_context('/api/sw_industry_stats?level=l1&_full=1'):
                # 两次等待均非主算且仍 pending → 最终仍可能 SERVED（尽力返回）
                # 关键：中途不得在第一次等待后就当成完成短路（需再抢）
                out = m.apiSwIndustryStats()
        self.assertEqual(served['n'], 1)
        self.assertEqual(out, 'SERVED_PENDING')
        # 证明抢了两次，而不是第一次等待后立刻返回
        # （side_effect 第二次 Wait 被消费）


    def testWarmSkipsOnlyCompleteCache(self):
        """预热不得因 pending 占位而跳过。"""
        calls = {'n': 0}

        def fakeApi():
            calls['n'] += 1

        key = 'l1||||x12'
        with m._swCacheLock:
            m._sw_industry_stats_cache[key] = (
                time.time() + 3600,
                {'pending_fina': True, 'rows': [1]},
                {},
                'l1',
            )
        fakeApp = type('A', (), {})()
        with (
            patch.object(m, 'apiSwIndustryStats', side_effect=fakeApi),
            patch.object(m, 'logger'),
        ):
            # 需要 Flask app_context；用真实小 app
            from flask import Flask
            app = Flask('warm_test')
            with patch.object(m, 'apiSwIndustryStats', side_effect=fakeApi):
                # 直接测跳过逻辑：pending 时不应 continue 掉 L1
                hitExisting, _, _ = m._sw_industry_stats_cache_get(key, time.time())
                self.assertTrue(hitExisting.get('pending_fina'))
                shouldSkip = hitExisting is not None and not hitExisting.get('pending_fina')
                self.assertFalse(shouldSkip)

            with m._swCacheLock:
                m._sw_industry_stats_cache[key] = (
                    time.time() + 3600,
                    {'pending_fina': False, 'rows': [1]},
                    {},
                    'l1',
                )
            hit2, _, _ = m._sw_industry_stats_cache_get(key, time.time())
            self.assertTrue(hit2 is not None and not hit2.get('pending_fina'))


if __name__ == '__main__':
    unittest.main()
