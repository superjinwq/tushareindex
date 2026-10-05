from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest import mock

from flask import Flask

from rt_monitor import monolith as m


def test_ma_signal_refresh_dispatches_builder_to_isolated_single_worker():
    expected = [{"ts_code": "510300.SH", "60MIN_20": "up"}]

    class FakeFuture:
        def result(self, timeout=None):
            assert timeout == m.ETF_MA_SIGNAL_PROCESS_TIMEOUT_SEC
            return expected

    class FakeExecutor:
        submitted = None

        def submit(self, fn):
            self.submitted = fn
            return FakeFuture()

    executor = FakeExecutor()
    with mock.patch.object(m, "read_etf_ma_signal_table_rows") as builder:
        rows = m._read_ma_signal_rows_for_refresh(executor=executor)

    assert rows == expected
    assert executor.submitted is builder
    builder.assert_not_called()


def test_ma_signal_refresh_process_isolation_has_runtime_rollback():
    expected = [{"ts_code": "510300.SH", "60MIN_20": "up"}]
    with (
        mock.patch.object(m, "ETF_MA_SIGNAL_PROCESS_ISOLATION", False),
        mock.patch.object(
            m, "read_etf_ma_signal_table_rows", return_value=expected
        ) as builder,
    ):
        rows = m._read_ma_signal_rows_for_refresh()

    assert rows == expected
    builder.assert_called_once_with()


def test_ma_signal_realtime_overlay_rebases_intraday_ranges_without_mutating_cache():
    cached = [{
        "ts_code": "510300.SH",
        "60MIN_20": "up",
        "etf_last": 4.0,
        "etf_pct": 1.0,
        "range_pct_t1": 0.5,
        "range_pct_t5": 10.0,
        "range_pct_t20": -20.0,
    }]
    quotes = {
        "510300.SH": {
            "etf_last": 4.4,
            "etf_pct": 2.0,
            "etf_gap": 0.3,
            "etf_intra": 1.7,
            "etf_amp": 2.2,
            "etf_amount_yi": 12.3,
            "etf_observed_at": "2026-09-23 11:20:01",
        }
    }

    rows, observed_at = m._overlay_ma_signal_realtime_rows(cached, quotes)

    assert rows[0]["60MIN_20"] == "up"
    assert rows[0]["etf_last"] == 4.4
    assert rows[0]["etf_pct"] == 2.0
    assert rows[0]["range_pct_t1"] == 0.5
    assert rows[0]["range_pct_t5"] == 21.0
    assert rows[0]["range_pct_t20"] == -12.0
    assert observed_at == "2026-09-23 11:20:01"
    assert cached[0]["etf_last"] == 4.0
    assert cached[0]["range_pct_t5"] == 10.0


def test_ma_signal_api_returns_truthful_signal_and_quote_timestamps():
    app = Flask(__name__)
    cached_at = datetime(2026, 9, 23, 11, 10, 0).timestamp()
    old_cache = m.etf_ma_signal_cache
    old_cache_ts = m.etf_ma_signal_last_read_ts
    old_refreshing = m.etf_ma_signal_refreshing
    try:
        m.etf_ma_signal_cache = [{
            "ts_code": "510300.SH",
            "60MIN_20": "up",
            "etf_last": 4.0,
            "range_pct_t5": 10.0,
        }]
        m.etf_ma_signal_last_read_ts = cached_at
        m.etf_ma_signal_refreshing = True
        live = {
            "510300.SH": {
                "etf_last": 4.4,
                "etf_observed_at": "2026-09-23 11:20:01",
            }
        }
        with mock.patch.object(m, "_fetch_rt_k_etf_quotes", return_value=live):
            with app.test_request_context("/api/etf_ma_signal_table"):
                payload = m.get_etf_ma_signal_table().get_json()
        assert payload["rows"][0]["etf_last"] == 4.4
        assert payload["rows"][0]["60MIN_20"] == "up"
        assert payload["quote_observed_at"] == "2026-09-23 11:20:01"
        assert payload["signal_updated_at"] == "2026-09-23 11:10:00"
        assert payload["last_update"] == "2026-09-23 11:20:01"
        assert payload["stale"] is True
    finally:
        m.etf_ma_signal_cache = old_cache
        m.etf_ma_signal_last_read_ts = old_cache_ts
        m.etf_ma_signal_refreshing = old_refreshing


def test_t_board_pauses_hidden_tab_and_recovers_hung_primary_request():
    html = Path("templates/rt_monitor_opt_t_board.html").read_text(encoding="utf-8")
    bg = html.split("function backgroundRefreshTick", 1)[1].split(
        "function scheduleBackgroundRefresh", 1
    )[0]
    delta = html.split("function loadQuoteDelta()", 1)[1].split(
        "function scheduleQuoteDeltaRefresh", 1
    )[0]
    load = html.split("function loadUnderlying", 1)[1].split("function onAddClick", 1)[0]

    assert "document.hidden" in bg
    assert "document.hidden" in delta
    assert "tBoardFetchController" in load
    assert "tBoardFetchController.abort()" in load
    assert "T_BOARD_FETCH_TIMEOUT_MS" in load
    assert "schedulePrimaryRetry" in load
    assert "visibilitychange" in html
    assert "loadQuoteDelta();" in html.split("visibilitychange", 1)[1]


def test_t_board_background_refresh_does_not_abort_and_retry_churn():
    html = Path("templates/rt_monitor_opt_t_board.html").read_text(encoding="utf-8")
    bg = html.split("function backgroundRefreshTick", 1)[1].split(
        "function scheduleBackgroundRefresh", 1
    )[0]

    assert "bgRefreshInflight" in bg
    assert "priority: 'low'" in bg
    assert "AbortController" not in bg
    assert ".abort()" not in bg
