"""休市冻结：财报宽表保活/旧值送出 + 申万实时层刷新间隔（交易时段行为不变）。"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.services import fina_arturn as finaSvc  # noqa: E402
from rt_monitor.services import fina_arturn_queries as finaQ  # noqa: E402
from rt_monitor.services import sw_industry as sw  # noqa: E402

_KEY = finaQ._fina_arturn_table_cache_key('20260630')


@pytest.fixture
def marketActive(monkeypatch):
    state = {'active': True}
    monkeypatch.setattr(finaQ, 'isAShareMarketActive', lambda now=None, closeGraceSec=0.0: state['active'])
    monkeypatch.setattr(sw, 'isAShareMarketActive', lambda now=None, closeGraceSec=0.0: state['active'])
    monkeypatch.setattr(finaQ, 'FINA_ARTURN_OFFHOURS_FREEZE', True)
    finaQ.setFinaServeStaleUntilRefreshed(False)
    finaQ._fina_indicator_metric_table_cache.clear()
    finaQ._fina_arturn_wire_cache.clear()
    yield state
    finaQ.setFinaServeStaleUntilRefreshed(False)
    finaQ._fina_indicator_metric_table_cache.clear()
    finaQ._fina_arturn_wire_cache.clear()


def _putExpired(payload: dict) -> None:
    finaQ._fina_indicator_metric_table_cache[_KEY] = (time.time() - 1, payload)


@pytest.mark.parametrize('wasActive, isActive, since, expected', [
    (True, True, 599.0, None),         # 交易时段：未到 600s 间隔不动（与旧版一致）
    (True, True, 600.0, '保活'),        # 交易时段：到间隔照常保活
    (False, False, 99999.0, None),     # 休市：无论多久都不重算
    (False, True, 0.0, '开盘保活'),     # 开盘/午后复市立即补一轮
    (True, False, 0.0, '收盘定格'),     # 收盘（含宽限）定格一次
])
def test_keepalive_state_machine(wasActive, isActive, since, expected) -> None:
    assert finaSvc._finaKeepaliveStep(wasActive, isActive, since, 600.0) == expected


def test_trading_session_expired_entry_not_served(marketActive) -> None:
    _putExpired({'rows': [1]})
    assert finaQ._finaCacheGetAllowStale(finaQ._fina_indicator_metric_table_cache, _KEY, time.time()) is None


def test_offhours_expired_entry_served_stale(marketActive) -> None:
    marketActive['active'] = False
    payload = {'rows': [1]}
    _putExpired(payload)
    assert finaQ._finaCacheGetAllowStale(finaQ._fina_indicator_metric_table_cache, _KEY, time.time()) is payload


def test_open_first_keepalive_serves_stale_until_refreshed(marketActive) -> None:
    payload = {'rows': [1]}
    _putExpired(payload)
    finaQ.setFinaServeStaleUntilRefreshed(True)
    assert finaQ._finaCacheGetAllowStale(finaQ._fina_indicator_metric_table_cache, _KEY, time.time()) is payload


def test_freeze_switch_off_restores_old_behavior(marketActive, monkeypatch) -> None:
    marketActive['active'] = False
    monkeypatch.setattr(finaQ, 'FINA_ARTURN_OFFHOURS_FREEZE', False)
    _putExpired({'rows': [1]})
    assert finaQ.isFinaArturnMarketActive() is True
    assert finaQ._finaCacheGetAllowStale(finaQ._fina_indicator_metric_table_cache, _KEY, time.time()) is None


def test_keepalive_rebuild_ignores_stale_even_offhours(marketActive, monkeypatch) -> None:
    marketActive['active'] = False
    _putExpired({'rows': ['old']})
    calls: list[str] = []

    def fakeQuery(ed):
        calls.append(ed)
        return {'rows': ['new'], 'last_update': 'x'}

    monkeypatch.setattr(finaQ, '_query_fina_arturn_table', fakeQuery)
    assert finaQ.getOrBuildFinaArturnTable('20260630')['rows'] == ['old']
    assert calls == []
    assert finaQ.getOrBuildFinaArturnTable('20260630', allowStale=False)['rows'] == ['new']
    assert calls == ['20260630']


def test_expire_table_cache_keeps_entries(marketActive) -> None:
    finaQ._fina_indicator_metric_table_cache[_KEY] = (time.time() + 999, {'rows': [1]})
    assert finaQ.expireFinaArturnTableCache() == 1
    assert finaQ._small_ttl_cache_get(finaQ._fina_indicator_metric_table_cache, _KEY, time.time()) is None
    assert _KEY in finaQ._fina_indicator_metric_table_cache


def test_wire_reencodes_when_table_rebuilt(marketActive) -> None:
    base = {'rows': [{'a': 1}], 'selected_end_date': '20260630', 'data_version': 'v', 'end_dates': ['20260630']}
    first = finaQ.ensureFinaArturnWireGzip({**base, 'last_update': '2026-10-04 15:05:00'})
    again = finaQ.ensureFinaArturnWireGzip({**base, 'last_update': '2026-10-04 15:05:00'})
    rebuilt = finaQ.ensureFinaArturnWireGzip({**base, 'last_update': '2026-10-04 15:06:00'})
    assert first is again
    assert rebuilt is not first
    assert len(finaQ._fina_arturn_wire_cache) == 1


def test_sw_live_ttl_unchanged_in_session(marketActive) -> None:
    assert sw._swLiveMapsOffhoursExtraSec() == 0.0


def test_sw_live_ttl_relaxed_offhours(marketActive, monkeypatch) -> None:
    marketActive['active'] = False
    monkeypatch.setattr(sw, 'SW_LIVE_OFFHOURS_REFRESH_SEC', 300.0)
    assert sw._swLiveMapsOffhoursExtraSec() + sw.SW_LIVE_MARKET_MAPS_CACHE_TTL == 300.0
    monkeypatch.setattr(sw, 'SW_LIVE_OFFHOURS_REFRESH_SEC', 0.0)
    assert sw._swLiveMapsOffhoursExtraSec() == 0.0


def test_sw_live_fresh_check_uses_offhours_extra() -> None:
    import inspect

    body = inspect.getsource(sw._sw_live_market_maps_is_fresh)
    assert '_swLiveMapsOffhoursExtraSec()' in body
    assert 'hit[0] + extraSec' in body

# 代码更新时间：2026-10-04 17:42 休市定格/申万放宽回归
