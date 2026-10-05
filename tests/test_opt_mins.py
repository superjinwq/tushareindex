#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opt_mins 纯函数单测（不打 Tushare、不写业务全量）。"""

import datetime as dt
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opt_mins import (  # noqa: E402
    FREQ,
    OptContract,
    SyncState,
    _processDf,
    buildFetchWindow,
    clampWorkers,
    incrementalFreshCutoff,
    isCoverageComplete,
    isFreshForIncremental,
    isSafeTestRun,
    parseExchanges,
    parseTsCodes,
    selectIncrementalContracts,
    shouldRefuseUnconstrainedAuto,
    shouldSkipContract,
    splitDatetimeRange,
    validate_before_save,
    yearSlices,
)


def test_parse_ts_codes_dedup():
    assert parseTsCodes(" 10007976.SH, 10007976.SH, MO2609-C-6600.CFX ") == [
        "10007976.SH",
        "MO2609-C-6600.CFX",
    ]
    assert parseTsCodes("") == []


def test_parse_exchanges_dedup():
    assert parseExchanges(" sse, SZSE,sse ") == ("SSE", "SZSE")
    assert parseExchanges("") == ()
    assert parseExchanges("CFFEX,DCE,SHFE,CZCE") == ("CFFEX", "DCE", "SHFE", "CZCE")


def test_select_incremental_keeps_incomplete_delist():
    today = dt.date(2026, 9, 6)
    lookback = dt.date(2026, 8, 31)
    expired = OptContract("X.DCE", "DCE", dt.date(2026, 6, 1), dt.date(2026, 8, 20))
    incomplete = SyncState("X.DCE", dt.datetime(2026, 8, 10, 15, 0, 0), 80, "partial")
    done = SyncState("X.DCE", dt.datetime(2026, 8, 20, 15, 0, 0), 120, "done")
    assert selectIncrementalContracts([expired], {"X.DCE": incomplete}, lookback, today) == [
        expired
    ]
    assert selectIncrementalContracts([expired], {"X.DCE": done}, lookback, today) == []


def test_clamp_workers():
    assert clampWorkers(0) == 1
    assert clampWorkers(5) == 5
    assert clampWorkers(99) == 16


def test_refuse_unconstrained_auto():
    assert shouldRefuseUnconstrainedAuto("auto", [], 0) is True
    assert shouldRefuseUnconstrainedAuto("full", [], 0) is False
    assert shouldRefuseUnconstrainedAuto("incremental", [], 0) is False
    assert shouldRefuseUnconstrainedAuto("auto", ["10007976.SH"], 0) is False
    assert shouldRefuseUnconstrainedAuto("auto", [], 3) is False
    assert isSafeTestRun([], 0) is False
    assert isSafeTestRun(["x"], 0) is True


def test_coverage_and_skip():
    today = dt.date(2026, 9, 4)
    expired = OptContract("A", "SSE", dt.date(2024, 9, 27), dt.date(2024, 11, 27))
    listed = OptContract("B", "CFFEX", dt.date(2025, 9, 22), dt.date(2026, 9, 18))
    doneState = SyncState("A", dt.datetime(2024, 11, 27, 15, 0, 0), 195, "done")
    emptyOld = SyncState("A", None, 0, "empty")
    partial = SyncState("B", dt.datetime(2026, 9, 3, 15, 0, 0), 10, "partial")

    assert isCoverageComplete(doneState.maxTradeTime, expired.delistDate) is True
    assert isCoverageComplete(partial.maxTradeTime, listed.delistDate) is False
    assert shouldSkipContract(expired, doneState, today, force=False) is True
    assert shouldSkipContract(expired, doneState, today, force=True) is False
    assert shouldSkipContract(expired, emptyOld, today, force=False) is True
    assert shouldSkipContract(listed, partial, today, force=False) is False
    assert shouldSkipContract(listed, None, today, force=False) is False

    friday = dt.date(2026, 9, 4)
    listedFresh = SyncState("B", dt.datetime(2026, 9, 4, 15, 0, 0), 20, "partial")
    cutoffDay = incrementalFreshCutoff(friday, now=dt.datetime(2026, 9, 4, 16, 0, 0))
    cutoffNight = incrementalFreshCutoff(friday, now=dt.datetime(2026, 9, 5, 10, 0, 0))
    assert cutoffDay == dt.datetime(2026, 9, 4, 15, 0, 0)
    assert cutoffNight == dt.datetime(2026, 9, 4, 21, 0, 0)
    assert isFreshForIncremental(listedFresh.maxTradeTime, cutoffDay) is True
    assert isFreshForIncremental(listedFresh.maxTradeTime, cutoffNight) is False
    assert (
        shouldSkipContract(
            listed,
            listedFresh,
            today,
            force=False,
            freshCutoff=cutoffDay,
            skipFresh=True,
        )
        is True
    )
    assert (
        shouldSkipContract(
            listed,
            listedFresh,
            today,
            force=False,
            freshCutoff=cutoffNight,
            skipFresh=True,
        )
        is False
    )
    nightBar = SyncState("B", dt.datetime(2026, 9, 4, 21, 0, 0), 22, "partial")
    assert (
        shouldSkipContract(
            listed,
            nightBar,
            today,
            force=False,
            freshCutoff=cutoffNight,
            skipFresh=True,
        )
        is True
    )


def test_build_fetch_window_night_lead_and_lookback():
    today = dt.date(2026, 9, 4)
    listed = dt.date(2026, 1, 29)
    delist = dt.date(2026, 9, 23)
    full = buildFetchWindow(listed, delist, None, today, 5)
    assert full is not None
    start, end = full
    assert start == dt.datetime(2026, 1, 28, 16, 0, 0)
    assert end == dt.datetime(2026, 9, 4, 23, 59, 59)

    localMax = dt.datetime(2026, 9, 3, 15, 0, 0)
    win2 = buildFetchWindow(listed, delist, localMax, today, 5)
    assert win2 is not None
    assert win2[0] == dt.datetime(2026, 8, 29, 15, 0, 0)


def test_year_and_split_slices():
    slices = yearSlices(dt.datetime(2024, 11, 1, 0, 0, 0), dt.datetime(2026, 2, 1, 0, 0, 0))
    assert len(slices) == 3
    assert slices[0][0].year == 2024
    halves = splitDatetimeRange(dt.datetime(2024, 1, 1), dt.datetime(2024, 12, 31))
    assert len(halves) == 2
    assert halves[0][1] == halves[1][0]


def test_process_df_ohlc_and_freq():
    raw = pd.DataFrame(
        [
            {
                "ts_code": " 10007976.SH ",
                "trade_time": "2024-09-27T15:00:00",
                "open": 0.0804,
                "high": 0.1429,
                "low": 0.0792,
                "close": 0.137,
                "vol": 2986.0,
                "amount": 3381540.0,
                "oi": 6499.0,
            }
        ]
    )
    out = _processDf(raw)
    assert out.loc[0, "ts_code"] == "10007976.SH"
    assert out.loc[0, "freq"] == FREQ
    assert out.loc[0, "trade_time"] == "2024-09-27 15:00:00"
    assert out.loc[0, "close"] == 0.137
    validate_before_save(out)


def test_validate_rejects_overflow():
    df = pd.DataFrame(
        [
            {
                "ts_code": "X.SH",
                "freq": FREQ,
                "open": 10**12,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "amount": 1.0,
                "oi": 1.0,
            }
        ]
    )
    with pytest.raises(ValueError, match="decimal overflow"):
        validate_before_save(df)

# 代码更新时间：2026-09-06 12:28
