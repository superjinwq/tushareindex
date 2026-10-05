# coding: utf-8
"""财报宽表：请求/预热不得改写共享缓存 dict；同口径并发首请求只 gzip 编码一次。"""
from __future__ import annotations

import threading
import time

import rt_monitor.services.fina_arturn as fa
import rt_monitor.services.fina_arturn_queries as finaQ


def _shared() -> dict:
    return {'rows': [{'ts_code': '600000.SH', 'p0': 1.0}], 'periods': ['20260630'],
            'contract_periods': ['20260630'], 'data_version': 'v1', 'last_update': '2026-10-04 22:00:00'}


def _patchQueries(monkeypatch, shared: dict) -> None:
    import rt_monitor.monolith as mono
    monkeypatch.setattr(mono, '_normalize_end_date_yyyymmdd', lambda raw: raw)
    monkeypatch.setattr(finaQ, '_query_arturn_end_dates', lambda limit_n=120: ['20260630', '20260331'])
    monkeypatch.setattr(finaQ, '_finaCacheGetAllowStale', lambda cache, key, now: shared)
    monkeypatch.setattr(finaQ, '_small_ttl_cache_get', lambda cache, key, now: None)
    monkeypatch.setattr(finaQ, 'getOrBuildFinaArturnTable', lambda ed, allowStale=True: shared)
    monkeypatch.setattr(finaQ, 'ensureFinaArturnWireGzip', lambda payload: b'gz')
    monkeypatch.setattr(finaQ, '_log_slow_api', lambda *a, **k: None)


def test_request_does_not_mutate_shared_cache_dict(monkeypatch) -> None:
    shared = _shared()
    snapshot = dict(shared)
    _patchQueries(monkeypatch, shared)
    payload, status, wire = fa.queryFinaArturnDaysTable('20260630')
    assert status == 200 and wire == b'gz'
    assert payload is not shared and payload['cache_hit'] is True
    assert payload['selected_end_date'] == '20260630' and payload['rows'] is shared['rows']
    assert list(payload)[-3:] == ['selected_end_date', 'end_dates', 'cache_hit']
    assert shared == snapshot


def test_warmup_does_not_mutate_shared_cache_dict(monkeypatch) -> None:
    shared = _shared()
    snapshot = dict(shared)
    _patchQueries(monkeypatch, shared)
    seen = []
    monkeypatch.setattr(finaQ, 'ensureFinaArturnWireGzip', lambda payload: seen.append(payload) or b'gz')
    monkeypatch.setattr(finaQ, 'FINA_ARTURN_WARMUP_PREFETCH_N', 1)
    fa._finaWarmRecentEndDates('预热')
    assert shared == snapshot
    assert seen and seen[0]['cache_hit'] is True and seen[0]['end_dates'] == ['20260630', '20260331']


def test_concurrent_first_requests_encode_once(monkeypatch) -> None:
    finaQ._fina_arturn_wire_cache.clear()
    calls = []
    real = finaQ._finaArturnEncodeWire

    def slowEncode(payload, key, stamp):
        calls.append(key)
        time.sleep(0.2)
        return real(payload, key, stamp)

    monkeypatch.setattr(finaQ, '_finaArturnEncodeWire', slowEncode)
    payload = dict(_shared(), selected_end_date='20260630', end_dates=['20260630'], cache_hit=False)
    out = []
    threads = [threading.Thread(target=lambda: out.append(finaQ.ensureFinaArturnWireGzip(payload)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    finaQ._fina_arturn_wire_cache.clear()
    assert len(calls) == 1
    assert len(out) == 8 and len({id(b) for b in out}) == 1

# 代码更新时间：2026-10-04 22:46 新建：财报共享缓存不被请求改写、并发只编码一次
