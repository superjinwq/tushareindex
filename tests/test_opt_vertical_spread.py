#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opt_vertical_spread_backtest 纯函数单测。"""
import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opt_vertical_spread_backtest import (  # noqa: E402
    classifyExpiries,
    pickLegs,
    simulateTrade,
    PER_UNIT,
)


def test_classify_expiries_basic():
    mats = [dt.date(2024, 1, 24), dt.date(2024, 2, 28), dt.date(2024, 3, 27), dt.date(2024, 6, 26)]
    out = classifyExpiries(dt.date(2024, 1, 15), mats)
    assert out["near"] == dt.date(2024, 1, 24)
    assert out["next"] == dt.date(2024, 2, 28)
    assert out["quarter"] == dt.date(2024, 3, 27)
    assert out["next_quarter"] == dt.date(2024, 6, 26)


def test_classify_expiries_overlap_quarter():
    # t 在 3 月(季月)：当月即当季
    mats = [dt.date(2024, 3, 27), dt.date(2024, 4, 24), dt.date(2024, 6, 26), dt.date(2024, 9, 25)]
    out = classifyExpiries(dt.date(2024, 3, 1), mats)
    assert out["near"] == dt.date(2024, 3, 27)
    assert out["quarter"] == dt.date(2024, 3, 27)
    assert out["next_quarter"] == dt.date(2024, 6, 26)


def test_classify_expiries_filters_past():
    mats = [dt.date(2024, 1, 24), dt.date(2024, 2, 28)]
    out = classifyExpiries(dt.date(2024, 2, 1), mats)
    assert out["near"] == dt.date(2024, 2, 28)
    assert out["next"] is None


def _chain():
    return pd.DataFrame([
        {"ts_code": "K38", "exercise_price": 3.8, "px": 0.30, "oi": 1000, "vol": 100},
        {"ts_code": "K39", "exercise_price": 3.9, "px": 0.22, "oi": 1000, "vol": 100},
        {"ts_code": "K40", "exercise_price": 4.0, "px": 0.15, "oi": 1000, "vol": 100},
        {"ts_code": "K41", "exercise_price": 4.1, "px": 0.10, "oi": 1000, "vol": 100},
        {"ts_code": "K42", "exercise_price": 4.2, "px": 0.06, "oi": 1000, "vol": 100},
    ])


def test_pick_legs_bull():
    # spot=4.0 → 买近平值 K40, 卖高 1 档 K41
    legs = pickLegs(_chain(), spot=4.0, side="bull", width=1)
    assert legs["buy_ts"] == "K40"
    assert legs["sell_ts"] == "K41"
    assert legs["buy_px"] > legs["sell_px"]  # 借记 > 0


def test_pick_legs_bear_width2():
    # spot=4.0 → 买 K40, 卖低 2 档 K38
    legs = pickLegs(_chain(), spot=4.0, side="bear", width=2)
    assert legs["buy_ts"] == "K40"
    assert legs["sell_ts"] == "K38"


def test_pick_legs_liquidity_filter():
    ch = _chain()
    ch.loc[ch["ts_code"] == "K41", "oi"] = 1  # 卖腿流动性不足
    assert pickLegs(ch, spot=4.0, side="bull", width=1) is None


def test_pick_legs_out_of_range():
    assert pickLegs(_chain(), spot=4.0, side="bull", width=10) is None


def test_simulate_trade_take_profit():
    idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    sp = pd.Series([0.10, 0.16, 0.20], index=idx)  # 建仓0.10, 涨到0.16触发1.5倍
    r = simulateTrade(0.10, sp, kTp=1.5, kSl=None, exitCutoff=idx[-1])
    assert r["exit_reason"] == "tp"
    assert abs(r["exit_spread"] - 0.15) < 1e-9
    # pnl = (0.15-0.10)*10000 - 成本
    assert r["pnl"] < (0.15 - 0.10) * PER_UNIT


def test_simulate_trade_stop_loss():
    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    sp = pd.Series([0.10, 0.04], index=idx)  # 跌到0.04 <= 0.10*0.5
    r = simulateTrade(0.10, sp, kTp=2.0, kSl=0.5, exitCutoff=idx[-1])
    assert r["exit_reason"] == "sl"
    assert r["pnl"] < 0


def test_simulate_trade_expire():
    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    sp = pd.Series([0.10, 0.12], index=idx)  # 既不止盈也不止损
    r = simulateTrade(0.10, sp, kTp=2.0, kSl=0.5, exitCutoff=idx[-1])
    assert r["exit_reason"] == "expire"
    assert abs(r["exit_spread"] - 0.12) < 1e-9


def test_simulate_trade_invalid_debit():
    idx = pd.to_datetime(["2024-01-02"])
    sp = pd.Series([0.10], index=idx)
    assert simulateTrade(0.0, sp, 1.5, 0.5, idx[-1]) is None
