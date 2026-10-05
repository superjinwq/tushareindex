# coding: utf-8
import unittest
from unittest.mock import patch
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest

from rt_monitor.services import opt_iv_windows as ivWindows
from rt_monitor.services.opt_sp_plan import expandingPctile, mergeLiveOptPxMap
from rt_monitor.services.opt_t_board import (
    DAQMT_QUOTE_DOWN_COOLDOWN_SEC,
    DAQMT_QUOTE_FETCH_TIMEOUT_SEC,
    DAQMT_QUOTE_MEMO_SEC,
    _optMetaUndlMatches,
    _optTBoardRowIsDisplay,
    _opt_t_board_build_obligation_selector,
    _opt_t_per_cells,
    _opt_t_board_format_expiry_meta,
    _opt_t_board_selector_sell_price,
    _fmt_opt_daily_qty,
    blendSectionQuoteAsof,
    decodeQuoteTickDate,
    isFreshDaqmtOptTick,
    mergeQmtChainGaps,
    overlayDaqmtQuotes,
    packChipSpotQuotes,
    _chipSpotIntraPp,
    _chipSpotGapPp,
    _chipGapPerLast,
    _chipRankPer,
    queryOptTBoardQuoteDelta,
    qmtMetaToOptBasicRow,
    qmtOptToTsCode,
)


def _ms(y: int, m: int, d: int, hh: int = 10, mm: int = 0) -> float:
    return datetime(y, m, d, hh, mm).timestamp() * 1000.0


class QmtCodeMapTest(unittest.TestCase):
    def testShoAndSzo(self):
        self.assertEqual(qmtOptToTsCode("10011485.SHO"), "10011485.SH")
        self.assertEqual(qmtOptToTsCode("90007481.szo"), "90007481.SZ")
        self.assertEqual(qmtOptToTsCode("588000.SH"), "588000.SH")


class OptTBoardQuoteDeltaTest(unittest.TestCase):
    """轻量接口只能读快照，且须分别报告观察与值变动时点。"""

    def testLightDeltaReturnsLiveFieldsAndSeparateObservationClock(self):
        now = datetime.now().replace(microsecond=0)
        nowMs = now.timestamp() * 1000.0
        code = "10010971.SHO"
        snapshot = {
            "ok": True,
            "optWall": now.isoformat(timespec="seconds"),
            "etf": {},
            "optMeta": {code: {"undl": "510050.SH", "k": 2.5, "cp": "C"}},
            "opt": {
                code: {
                    "last": 0.1234,
                    "vol": 1200,
                    "oi": 54321,
                    "bid1": 0.1230,
                    "ask1": 0.1235,
                    "time": nowMs,
                    "lastTime": nowMs,
                    "qtyTime": nowMs,
                    "oiTime": nowMs,
                    "bookTime": nowMs,
                    "lastChangedTime": nowMs,
                    "qtyChangedTime": nowMs,
                    "oiChangedTime": nowMs,
                    "bookChangedTime": nowMs,
                    "fromTdx": True,
                }
            },
        }
        out = queryOptTBoardQuoteDelta("510050", snapshot=snapshot, now=now)

        self.assertTrue(out["ok"])
        self.assertEqual(out["u"], "510050")
        self.assertEqual(out["observed_at"], now.isoformat(timespec="seconds"))
        quote = out["quotes"]["10010971.SH"]
        self.assertEqual(quote["last"]["text"], "0.1234")
        self.assertEqual(quote["vol"]["text"], "1200")
        self.assertEqual(quote["oi"]["text"], "5.4万")
        self.assertEqual(quote["book"]["bid1"], "0.1230")
        self.assertEqual(quote["book"]["ask1"], "0.1235")
        self.assertEqual(quote["last"]["observed_at"], out["observed_at"])
        self.assertTrue(quote["last"]["changed_at"])
        self.assertIn("spots", out)

    def testChipSpotQuotesUseThreeDecimalsAndSignedPct(self):
        snapshot = {
            "ok": True,
            "etf": {
                "510050.SH": {
                    "last": 2.7489,
                    "lastClose": 2.7600,
                    "vol": 120000,
                    "open": 2.75,
                    "high": 2.76,
                    "low": 2.74,
                },
                "159915.SZ": {
                    "last": 3.012,
                    "preClose": 2.980,
                    "vol": 80000,
                    "open": 2.99,
                    "high": 3.02,
                    "low": 2.97,
                },
            },
        }
        spots = packChipSpotQuotes(snapshot)
        self.assertEqual(spots["510050"]["last_text"], "2.749")
        self.assertEqual(spots["510050"]["pct_text"], "-0.40")
        self.assertLess(spots["510050"]["pct"], 0)
        self.assertEqual(spots["159915"]["last_text"], "3.012")
        self.assertEqual(spots["159915"]["pct_text"], "+1.07")
        self.assertTrue(spots["159915"]["fresh"])
        self.assertEqual(spots["510050"]["intra_text"], "-0.04")
        self.assertEqual(spots["159915"]["intra_text"], "+0.74")

        now = datetime.now().replace(microsecond=0)
        out = queryOptTBoardQuoteDelta("510050", snapshot=snapshot, now=now)
        self.assertEqual(out["spots"]["510050"]["last_text"], "2.749")
        self.assertEqual(out["spots"]["159915"]["pct_text"], "+1.07")
        self.assertEqual(out["spots"]["510050"]["intra_text"], "-0.04")

    def testChipSpotIntraMatchesMaSignalFormula(self):
        """Intra=(最新−开盘)/开盘×100，与 monolith._fetch_rt_k_etf_quotes 同式。"""
        last, openPx = 3.015, 3.000
        expect = (last - openPx) / openPx * 100.0
        self.assertAlmostEqual(_chipSpotIntraPp(last, openPx), expect)
        self.assertIsNone(_chipSpotIntraPp(3.015, 0.0))
        self.assertIsNone(_chipSpotIntraPp(3.015, None))
        spots = packChipSpotQuotes({
            "ok": True,
            "etf": {
                "588000.SH": {
                    "last": last,
                    "lastClose": 3.000,
                    "open": openPx,
                    "vol": 1000,
                    "high": 3.02,
                    "low": 2.99,
                },
            },
        })
        self.assertEqual(spots["588000"]["intra_text"], "+0.50")
        self.assertEqual(spots["588000"]["gap_text"], "+0.00")

    def testChipSpotGapMatchesMaSignalFormula(self):
        """Gap=(开盘−昨收)/昨收×100，与主页 Ma/Ema 同式。"""
        self.assertAlmostEqual(_chipSpotGapPp(1.790, 1.749), (1.790 - 1.749) / 1.749 * 100.0)
        self.assertIsNone(_chipSpotGapPp(0.0, 1.758))
        self.assertIsNone(_chipSpotGapPp(None, 1.758))
        self.assertIsNone(_chipSpotGapPp(1.763, 0.0))
        spots = packChipSpotQuotes({
            "ok": True,
            "etf": {
                "588000.SH": {
                    "last": 1.763,
                    "lastClose": 1.749,
                    "open": 1.790,
                    "vol": 1000,
                    "high": 1.80,
                    "low": 1.74,
                },
            },
        })
        self.assertEqual(spots["588000"]["gap_text"], "+2.34")

    def testChipGapPerLastMatchesSpExpanding(self):
        """chip Gap Per 最后一点必须等于计划区 expandingPctile。"""
        rng = np.random.default_rng(0)
        hist = rng.normal(0.0, 0.8, 220)
        today = 2.34
        vals = np.append(hist, today)
        sp = expandingPctile(vals)
        got = _chipGapPerLast(vals.tolist())
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, float(sp[-1]), places=10)
        self.assertGreaterEqual(got, 95.0)

    def testChipSpotGapPerUsesInjectedHist(self):
        hist = [0.0] * 200
        from rt_monitor.services import opt_t_board as tboardMod
        tboardMod._chipGapPerValMemo.clear()
        tboardMod._chipDirHistMemo.clear()
        with patch(
            "rt_monitor.services.opt_t_board._chipGapHistFor",
            return_value=hist,
        ):
            spots = packChipSpotQuotes({
                "ok": True,
                "etf": {
                    "588000.SH": {
                        "last": 1.763,
                        "lastClose": 1.749,
                        "open": 1.790,
                        "vol": 1000,
                        "high": 1.80,
                        "low": 1.74,
                    },
                },
            })
        self.assertEqual(spots["588000"]["gap_text"], "+2.34")
        self.assertIsNone(spots["588000"]["gap_p"])
        self.assertIsNone(spots["588000"]["amp_p"])
        self.assertIsNone(spots["588000"]["pct_p"])
        self.assertIsNone(spots["588000"]["intra_p"])

    def testChipRankPerMatchesHomepageStrictLess(self):
        sample = [float(i) for i in range(100)]
        self.assertEqual(_chipRankPer(sample, -1.0), 0)
        self.assertEqual(_chipRankPer(sample, 4.0), 4)
        self.assertEqual(_chipRankPer(sample, 95.0), 95)
        self.assertEqual(_chipRankPer(sample, 200.0), 100)
        self.assertIsNone(_chipRankPer([], 1.0))
        self.assertIsNone(_chipRankPer(sample, None))

    def testChipDirPerReadsInjectedHomepageSample(self):
        from rt_monitor.services import opt_t_board as tboardMod
        sample = [float(i) for i in range(100)]
        tboardMod._chipDirHistMemo['588000'] = (
            0.0,
            date.today().isoformat(),
            {'pct': list(sample), 'intra': list(sample)},
        )
        try:
            with patch(
                'rt_monitor.services.opt_t_board._chipGapHistFor',
                return_value=[],
            ):
                self.assertEqual(tboardMod._chipDirPerOf('588000', 'pct', 4.0), 4)
                spots = packChipSpotQuotes({
                    'ok': True,
                    'etf': {
                        '588000.SH': {
                            'last': 1.763,
                            'lastClose': 1.749,
                            'open': 1.790,
                            'vol': 1000,
                        },
                    },
                })
            self.assertEqual(spots['588000']['pct_p'], 1)
            self.assertEqual(spots['588000']['pct_p_text'], '1')
            self.assertEqual(spots['588000']['intra_p'], 0)
            self.assertEqual(spots['588000']['intra_p_text'], '0')
        finally:
            tboardMod._chipDirHistMemo.pop('588000', None)

    def testChipGapAmpPerUsesHomepageSample(self):
        from rt_monitor.services import opt_t_board as tboardMod
        zeros = [0.0] * 200
        tboardMod._chipGapPerValMemo.clear()
        tboardMod._chipDirHistMemo['588000'] = (
            0.0,
            date.today().isoformat(),
            {'pct': [], 'intra': [], 'gap': list(zeros), 'amp': list(zeros)},
        )
        try:
            with patch(
                'rt_monitor.services.opt_t_board._chipGapHistFor',
                return_value=zeros,
            ):
                expanding = tboardMod._chipGapPerOf('588000', 2.34)
                spots = packChipSpotQuotes({
                    'ok': True,
                    'etf': {
                        '588000.SH': {
                            'last': 1.763,
                            'lastClose': 1.749,
                            'open': 1.790,
                            'high': 1.80,
                            'low': 1.74,
                            'vol': 1000,
                        },
                    },
                })
            self.assertGreaterEqual(expanding, 95.0)
            self.assertEqual(spots['588000']['gap_text'], '+2.34')
            self.assertEqual(spots['588000']['gap_p'], 100)
            self.assertEqual(spots['588000']['gap_p_text'], '100')
            self.assertEqual(spots['588000']['amp_text'], '3.43')
            self.assertEqual(spots['588000']['amp_p'], 100)
            self.assertEqual(spots['588000']['amp_p_text'], '100')
        finally:
            tboardMod._chipDirHistMemo.pop('588000', None)

    def testChipSpotAfterClosePrefersTodayRtkOverDaily(self):
        now = datetime(2026, 9, 30, 19, 40)
        rtk = {
            "588000": {
                "last_text": "1.616",
                "source": "rt_k",
                "asof_text": "09-30 16:29",
                "fresh": False,
            }
        }
        daily = {
            "588000": {"last_text": "1.657", "source": "daily", "asof_text": "09-29"},
        }
        with patch(
            "rt_monitor.services.opt_t_board._chipSpotRtkFallback",
            return_value=rtk,
        ) as rtkFn, patch(
            "rt_monitor.services.opt_t_board._chipSpotDailyFallback",
            return_value=daily,
        ) as dailyFn:
            spots = packChipSpotQuotes({"ok": True, "etf": {}}, now=now)
        self.assertEqual(spots["588000"]["source"], "rt_k")
        self.assertEqual(spots["588000"]["last_text"], "1.616")
        rtkFn.assert_called()
        self.assertNotIn("588000", dailyFn.call_args[0][0])

    def testChipSpotSessionDoesNotUseRtk(self):
        now = datetime(2026, 9, 30, 10, 0)
        with patch(
            "rt_monitor.services.opt_t_board._chipSpotRtkFallback",
            return_value={"588000": {"last_text": "1.616", "source": "rt_k"}},
        ) as rtkFn, patch(
            "rt_monitor.services.opt_t_board._chipSpotDailyFallback",
            return_value={"588000": {"last_text": "1.657", "source": "daily", "asof_text": "09-29"}},
        ):
            spots = packChipSpotQuotes({"ok": True, "etf": {}}, now=now)
        rtkFn.assert_not_called()
        self.assertEqual(spots["588000"]["source"], "daily")

    def testChipSpotIgnoresYesterdayQmtTick(self):
        now = datetime(2026, 9, 30, 19, 40)
        with patch(
            "rt_monitor.services.opt_t_board._chipSpotRtkFallback",
            return_value={"588000": {"last_text": "1.616", "source": "rt_k"}},
        ), patch(
            "rt_monitor.services.opt_t_board._chipSpotDailyFallback",
            return_value={},
        ):
            spots = packChipSpotQuotes({
                "ok": True,
                "etf": {
                    "588000.SH": {
                        "last": 1.700,
                        "lastClose": 1.657,
                        "open": 1.680,
                        "vol": 1000,
                        "high": 1.71,
                        "low": 1.66,
                        "t": "2026-09-29 15:00:00",
                    },
                },
            }, now=now)
        self.assertEqual(spots["588000"]["source"], "rt_k")
        self.assertEqual(spots["588000"]["last_text"], "1.616")

    def testLightDeltaRejectsOtherUnderlyingAndStaleOiClock(self):
        now = datetime.now().replace(microsecond=0)
        stale = now.replace(year=now.year - 1)
        nowMs = now.timestamp() * 1000.0
        staleMs = stale.timestamp() * 1000.0
        snapshot = {
            "ok": True,
            "optWall": now.isoformat(timespec="seconds"),
            "etf": {},
            "optMeta": {
                "10010971.SHO": {"undl": "510050.SH"},
                "90007082.SZO": {"undl": "159915.SZ"},
            },
            "opt": {
                "10010971.SHO": {
                    "last": 0.12,
                    "vol": 10,
                    "oi": 99,
                    "time": nowMs,
                    "lastTime": nowMs,
                    "qtyTime": nowMs,
                    "oiTime": staleMs,
                    "bookTime": nowMs,
                },
                "90007082.SZO": {
                    "last": 0.11,
                    "vol": 12,
                    "oi": 88,
                    "time": nowMs,
                    "lastTime": nowMs,
                    "qtyTime": nowMs,
                    "oiTime": nowMs,
                    "bookTime": nowMs,
                },
            },
        }
        out = queryOptTBoardQuoteDelta("510050", snapshot=snapshot, now=now)

        self.assertEqual(set(out["quotes"]), {"10010971.SH"})
        self.assertFalse(out["quotes"]["10010971.SH"]["oi"]["replace"])

    def testLightDeltaDoesNotAttributeMissingOiToTdx(self):
        """TDX 原始快照未给 OI 时，字段来源必须是未知而非 TDX。"""
        now = datetime.now().replace(microsecond=0)
        nowMs = now.timestamp() * 1000.0
        code = "10010971.SHO"
        snapshot = {
            "ok": True,
            "optMeta": {code: {"undl": "510050.SH"}},
            "opt": {
                code: {
                    "last": 0.12,
                    "vol": 10,
                    "bid1": 0.119,
                    "ask1": 0.121,
                    "fromTdx": True,
                    "lastSource": "tdx",
                    "qtySource": "tdx",
                    "bookSource": "tdx",
                    "time": nowMs,
                    "lastTime": nowMs,
                    "qtyTime": nowMs,
                    "bookTime": nowMs,
                }
            },
        }

        quote = queryOptTBoardQuoteDelta("510050", snapshot=snapshot, now=now)["quotes"][
            "10010971.SH"
        ]

        self.assertEqual(quote["oi"]["text"], "—")
        self.assertIsNone(quote["oi"]["source"])
        self.assertEqual(quote["last"]["source"], "tdx")
        self.assertEqual(quote["book"]["source"], "tdx")


class DecodeTickDateTest(unittest.TestCase):
    def testMsAndSec(self):
        ms = _ms(2026, 8, 21, 10, 10)
        self.assertEqual(decodeQuoteTickDate(ms), date(2026, 8, 21))
        self.assertEqual(decodeQuoteTickDate(ms / 1000.0), date(2026, 8, 21))

    def testIgnoreTinyAndIso(self):
        self.assertIsNone(decodeQuoteTickDate(1))
        self.assertEqual(decodeQuoteTickDate("2026-08-21 10:10:00"), date(2026, 8, 21))

    def testNumericStringMs(self):
        self.assertEqual(decodeQuoteTickDate("1787278210000"), date(2026, 8, 21))
        self.assertEqual(decodeQuoteTickDate("1787640849000"), date(2026, 8, 25))


class FreshTickTest(unittest.TestCase):
    def testNoTimestampRejected(self):
        self.assertFalse(
            isFreshDaqmtOptTick({"last": 0.05, "vol": 10}, date(2026, 8, 25))
        )

    def testStaleDayWithVolumeRejected(self):
        packed = {"last": 0.0167, "vol": 252927, "t": _ms(2026, 8, 21, 10, 10)}
        self.assertFalse(isFreshDaqmtOptTick(packed, date(2026, 8, 25)))

    def testVolZeroTodayRejected(self):
        packed = {
            "last": 1.762,
            "vol": 0,
            "lastClose": 1.762,
            "bid1": 2.07,
            "ask1": 2.11,
            "t": _ms(2026, 8, 25, 15, 0),
        }
        self.assertFalse(isFreshDaqmtOptTick(packed, date(2026, 8, 25), dailyClose=1.762))

    def testLastCloseMismatchDoesNotBlock(self):
        packed = {
            "last": 0.0167,
            "vol": 10,
            "lastClose": 0.1962,
            "t": _ms(2026, 8, 25, 10, 0),
        }
        self.assertTrue(
            isFreshDaqmtOptTick(packed, date(2026, 8, 25), dailyClose=0.1553)
        )

    def testTodayMatchingLastCloseKept(self):
        packed = {
            "last": 0.16,
            "vol": 10,
            "lastClose": 0.1553,
            "t": _ms(2026, 8, 25, 13, 0),
        }
        self.assertTrue(
            isFreshDaqmtOptTick(packed, date(2026, 8, 25), dailyClose=0.1553)
        )


class OverlayTest(unittest.TestCase):
    def testStaleDayQmtDoesNotOverwriteDailyOrClaimAnyFieldLive(self):
        quoteMap = {
            "10010971.SH": {
                "close": 0.10,
                "daily_close": 0.10,
                "vol": 321,
                "oi": 456,
                "trade_date": date(2026, 9, 10),
            }
        }
        snap = {
            "opt": {
                "10010971.SHO": {
                    "last": 0.05,
                    "vol": 10,
                    "oi": 99,
                    "bid1": 0.049,
                    "ask1": 0.051,
                    "t": _ms(2026, 9, 10, 15, 0),
                }
            },
            "optMeta": {"10010971.SHO": {"undl": "588000.SH"}},
        }
        out, _spot, n, src = overlayDaqmtQuotes(
            quoteMap, 1.0, "588000", snap, asof=date(2026, 9, 11)
        )
        row = out["10010971.SH"]
        self.assertEqual(src, "opt_daily")
        self.assertEqual(n, 0)
        self.assertEqual(row.get("close"), 0.10)
        self.assertEqual(row.get("vol"), 0)
        self.assertEqual(row.get("oi"), 456)
        self.assertEqual(row.get("trade_date"), date(2026, 9, 10))
        self.assertFalse(row.get("live"))
        self.assertFalse(row.get("trade_live"))
        self.assertFalse(row.get("book_live"))
        self.assertFalse(row.get("oi_live"))

    def testStaleDayTdxBookDoesNotBecomeTodayLive(self):
        quoteMap = {
            "10010971.SH": {
                "close": 0.10,
                "daily_close": 0.10,
                "vol": 321,
                "oi": 456,
                "trade_date": date(2026, 9, 10),
            }
        }
        snap = {
            "opt": {
                "10010971.SHO": {
                    "last": 0.05,
                    "vol": 10,
                    "oi": 99,
                    "bid1": 0.049,
                    "ask1": 0.051,
                    "fromTdx": True,
                    "t": _ms(2026, 9, 10, 15, 0),
                }
            },
            "optMeta": {"10010971.SHO": {"undl": "510050.SH"}},
        }
        out, _spot, n, src = overlayDaqmtQuotes(
            quoteMap, 3.0, "510050", snap, asof=date(2026, 9, 11)
        )
        row = out["10010971.SH"]
        self.assertEqual(src, "opt_daily")
        self.assertEqual(n, 0)
        self.assertEqual(row.get("close"), 0.10)
        self.assertEqual(row.get("vol"), 0)
        self.assertEqual(row.get("oi"), 456)
        self.assertEqual(row.get("trade_date"), date(2026, 9, 10))
        self.assertFalse(row.get("live"))
        self.assertFalse(row.get("trade_live"))
        self.assertFalse(row.get("book_live"))
        self.assertFalse(row.get("oi_live"))

    def testFreshQtyCannotMakeStaleTdxBookLive(self):
        quoteMap = {
            "10010971.SH": {
                "close": 0.10,
                "daily_close": 0.10,
                "vol": 1,
                "oi": 2,
                "trade_date": date(2026, 9, 10),
            }
        }
        snap = {
            "opt": {
                "10010971.SHO": {
                    "last": 0.12,
                    "vol": 12,
                    "oi": 22,
                    "bid1": 0.119,
                    "ask1": 0.121,
                    "fromTdx": True,
                    "lastSource": "qmt",
                    "qtySource": "qmt",
                    "oiSource": "qmt",
                    "bookSource": "tdx",
                    "lastTime": _ms(2026, 9, 11, 10, 0),
                    "qtyTime": _ms(2026, 9, 11, 10, 0),
                    "bookTime": _ms(2026, 9, 10, 15, 0),
                    "t": _ms(2026, 9, 11, 10, 0),
                }
            },
            "optMeta": {"10010971.SHO": {"undl": "510050.SH"}},
        }
        out, _spot, n, src = overlayDaqmtQuotes(
            quoteMap, 3.0, "510050", snap, asof=date(2026, 9, 11)
        )
        row = out["10010971.SH"]
        self.assertEqual(src, "daqmt")
        self.assertEqual(n, 1)
        self.assertEqual(row.get("last"), 0.12)
        self.assertEqual(row.get("vol"), 12)
        self.assertEqual(row.get("oi"), 22)
        self.assertTrue(row.get("trade_live"))
        self.assertTrue(row.get("oi_live"))
        self.assertFalse(row.get("book_live"))
        self.assertFalse(row.get("has_book"))
        self.assertIsNone(row.get("bid1"))
        self.assertIsNone(row.get("ask1"))

    def testOverlayLastAndSpot(self):
        quoteMap = {
            "10010971.SH": {
                "close": 0.10,
                "vol": 1,
                "oi": 2,
                "trade_date": date(2026, 8, 21),
            }
        }
        snap = {
            "etf": {"588000.SH": {"last": 1.23}},
            "opt": {
                "10010971.SHO": {
                    "last": 0.05,
                    "vol": 10,
                    "oi": 99,
                    "t": _ms(2026, 8, 24, 10, 0),
                }
            },
        }
        out, spot, n, src = overlayDaqmtQuotes(
            quoteMap, 1.0, "588000", snap, asof=date(2026, 8, 24)
        )
        self.assertEqual(src, "daqmt")
        self.assertEqual(n, 1)
        self.assertEqual(spot, 1.23)
        self.assertEqual(out["10010971.SH"]["close"], 0.05)
        self.assertEqual(out["10010971.SH"]["vol"], 10)
        self.assertEqual(out["10010971.SH"]["oi"], 99)
        self.assertEqual(out["10010971.SH"]["trade_date"], date(2026, 8, 24))
        self.assertTrue(out["10010971.SH"]["live"])

    def testOptBasicRowKeepsSingleKeyWhenQmtOverlays(self):
        quoteMap = {
            "10012371.SH": {
                "close": 0.0456,
                "vol": 0,
                "oi": 0,
                "trade_date": date(2026, 8, 26),
            }
        }
        snap = {
            "etf": {"588000.SH": {"last": 1.79}},
            "opt": {
                "10012371.SHO": {
                    "last": 0.0512,
                    "vol": 11753,
                    "oi": 6513,
                    "t": _ms(2026, 8, 27, 10, 0),
                }
            },
            "optMeta": {
                "10012371.SHO": {
                    "undl": "588000.SH",
                    "k": 1.9,
                    "exp": "2026-10-28",
                    "cp": "C",
                    "unit": 10000,
                }
            },
        }
        out, _spot, n, src = overlayDaqmtQuotes(
            quoteMap, 1.78, "588000", snap, asof=date(2026, 8, 27)
        )
        self.assertEqual(src, "daqmt")
        self.assertEqual(n, 1)
        self.assertEqual(set(out), {"10012371.SH"})
        self.assertEqual(out["10012371.SH"]["close"], 0.0512)
        self.assertEqual(out["10012371.SH"]["vol"], 11753)
        self.assertEqual(out["10012371.SH"]["oi"], 6513)

    def testSkipWhenNoHit(self):
        quoteMap = {"10010971.SH": {"close": 0.10, "vol": 1, "oi": 2}}
        snap = {"etf": {}, "opt": {"10000000.SHO": {"last": 0.01}}}
        out, spot, n, src = overlayDaqmtQuotes(quoteMap, 1.0, "588000", snap)
        self.assertEqual(src, "opt_daily")
        self.assertEqual(n, 0)
        self.assertEqual(spot, 1.0)
        self.assertEqual(out["10010971.SH"]["close"], 0.10)
        self.assertNotIn("10000000.SH", out)

    def testOverlaySkipsOtherUndl(self):
        quoteMap = {"10010971.SH": {"close": 0.10, "vol": 1, "oi": 2}}
        snap = {
            "etf": {"588000.SH": {"last": 1.23}},
            "opt": {
                "10010971.SHO": {
                    "last": 0.05,
                    "vol": 10,
                    "oi": 99,
                    "t": _ms(2026, 8, 24, 10, 0),
                },
                "10055555.SHO": {
                    "last": 0.99,
                    "vol": 50,
                    "oi": 1,
                    "t": _ms(2026, 8, 24, 10, 0),
                },
            },
            "optMeta": {
                "10055555.SHO": {
                    "undl": "510500",
                    "k": 7.5,
                    "exp": "2026-09-23",
                    "cp": "C",
                    "unit": 10000,
                }
            },
        }
        out, _spot, n, src = overlayDaqmtQuotes(
            quoteMap, 1.0, "588000", snap, asof=date(2026, 8, 24)
        )
        self.assertEqual(src, "daqmt")
        self.assertEqual(n, 1)
        self.assertNotIn("10055555.SH", out)
        self.assertEqual(out["10010971.SH"]["close"], 0.05)

    def testCffexUntouched(self):
        quoteMap = {"IO2509-C-4000.CFX": {"close": 100}}
        snap = {"etf": {"000300.SH": {"last": 4000}}, "opt": {}}
        _out, spot, n, src = overlayDaqmtQuotes(quoteMap, 3800.0, "000300", snap)
        self.assertEqual(src, "opt_daily")
        self.assertEqual(n, 0)
        self.assertEqual(spot, 3800.0)

    def testNoTradeUsesYesterdayClose(self):
        quoteMap = {
            "10012253.SH": {"close": 0.1553, "vol": 1, "oi": 2},
            "10010971.SH": {"close": 0.10, "vol": 1, "oi": 2},
        }
        snap = {
            "etf": {"588000.SH": {"last": 1.70}},
            "opt": {
                "10012253.SHO": {
                    "last": 0.2041,
                    "vol": 0,
                    "oi": 99,
                    "lastClose": 0.1962,
                    "t": _ms(2026, 8, 21, 10, 10),
                },
                "10010971.SHO": {
                    "last": 0.12,
                    "vol": 8,
                    "oi": 10,
                    "lastClose": 0.10,
                    "t": _ms(2026, 8, 25, 14, 0),
                },
            },
        }
        out, spot, n, src = overlayDaqmtQuotes(
            quoteMap, 1.0, "588000", snap, asof=date(2026, 8, 25)
        )
        self.assertEqual(src, "daqmt")
        self.assertEqual(n, 1)
        self.assertEqual(spot, 1.70)
        self.assertEqual(out["10012253.SH"]["close"], 0.1553)
        self.assertFalse(out["10012253.SH"].get("live"))
        self.assertEqual(out["10012253.SH"]["vol"], 0)
        self.assertEqual(out["10012253.SH"]["oi"], 2)
        self.assertFalse(out["10012253.SH"].get("oi_live"))
        self.assertEqual(out["10010971.SH"]["close"], 0.12)
        self.assertTrue(out["10010971.SH"]["live"])

    def testSessionVolumeUsesQmtLast(self):
        """有成交：最新/量/仓都用 QMT，不再用昨收冒充。"""
        quoteMap = {
            "10012153.SH": {
                "close": 0.0201,
                "vol": 192244,
                "oi": 38630,
                "trade_date": date(2026, 8, 24),
            }
        }
        snap = {
            "etf": {"588000.SH": {"last": 1.69}},
            "opt": {
                "10012153.SHO": {
                    "last": 0.0167,
                    "vol": 252927,
                    "oi": 46941,
                    "lastClose": 0.0201,
                    "t": "1787587200000",
                }
            },
        }
        out, spot, n, src = overlayDaqmtQuotes(
            quoteMap, 1.6, "588000", snap, asof=date(2026, 8, 25)
        )
        self.assertEqual(src, "daqmt")
        self.assertEqual(n, 1)
        self.assertEqual(spot, 1.69)
        self.assertEqual(out["10012153.SH"]["close"], 0.0167)
        self.assertTrue(out["10012153.SH"].get("live"))
        self.assertEqual(out["10012153.SH"]["vol"], 252927)
        self.assertEqual(out["10012153.SH"]["oi"], 46941)

    def testAllStaleStillOverlaysEtfSpot(self):
        quoteMap = {"10012253.SH": {"close": 0.1553, "vol": 1, "oi": 2}}
        snap = {
            "etf": {"159915.SZ": {"last": 3.21}},
            "opt": {
                "10012253.SHO": {
                    "last": 0.20,
                    "vol": 0,
                    "t": _ms(2026, 8, 21, 10, 11),
                }
            },
        }
        out, spot, n, src = overlayDaqmtQuotes(
            quoteMap, 2.0, "159915", snap, asof=date(2026, 8, 25)
        )
        self.assertEqual(src, "opt_daily")
        self.assertEqual(n, 0)
        self.assertEqual(spot, 3.21)
        self.assertEqual(out["10012253.SH"]["close"], 0.1553)
        self.assertEqual(out["10012253.SH"]["vol"], 0)

    def testOverlayInsertsMissingQuoteMap(self):
        """QMT 有、opt_daily 无：仍写入 quoteMap，供新挂牌显示。"""
        snap = {
            "etf": {"588000.SH": {"last": 1.70}},
            "opt": {
                "10019999.SHO": {
                    "last": 0.0123,
                    "vol": 10,
                    "oi": 5,
                    "t": _ms(2026, 8, 25, 10, 0),
                }
            },
            "optMeta": {
                "10019999.SHO": {
                    "undl": "588000",
                    "k": 1.85,
                    "exp": "2026-08-26",
                    "cp": "C",
                    "unit": 10000,
                }
            },
        }
        out, spot, n, src = overlayDaqmtQuotes(
            {}, 1.0, "588000", snap, asof=date(2026, 8, 25)
        )
        self.assertEqual(src, "daqmt")
        self.assertEqual(n, 1)
        self.assertEqual(spot, 1.70)
        self.assertEqual(out["10019999.SH"]["close"], 0.0123)
        self.assertTrue(out["10019999.SH"]["live"])
        self.assertEqual(out["10019999.SH"]["vol"], 10)
        self.assertEqual(out["10019999.SH"]["oi"], 5)


class BlendAsofTest(unittest.TestCase):
    def testPartialAndDaily(self):
        calls = {1.7: {"ts_code": "A.SH"}, 1.8: {"ts_code": "B.SH"}}
        puts = {1.7: {"ts_code": "C.SH"}}
        quoteMap = {
            "A.SH": {"live": True},
            "B.SH": {"close": 0.1},
            "C.SH": {"live": True},
        }
        text, kind = blendSectionQuoteAsof(
            calls, puts, quoteMap, "2026-08-24", "实时 14:32:01"
        )
        self.assertEqual(kind, "partial")
        self.assertEqual(text, "实时 14:32:01 · 2/3张")
        text2, kind2 = blendSectionQuoteAsof(
            calls, puts, {"A.SH": {}, "B.SH": {}, "C.SH": {}}, "2026-08-24", "实时"
        )
        self.assertEqual(kind2, "daily")
        self.assertEqual(text2, "无实时 · 2026-08-24")


class MergeLivePxTest(unittest.TestCase):
    def testSkipStaleKeepDaily(self):
        daily = {"10012253.SH": 0.1553, "10010971.SH": 0.10}
        optSnap = {
            "10012253.SHO": {
                "last": 0.2041,
                "vol": 0,
                "lastClose": 0.1962,
                "t": _ms(2026, 8, 21, 10, 10),
            },
            "10010971.SHO": {
                "last": 0.12,
                "vol": 3,
                "lastClose": 0.10,
                "t": _ms(2026, 8, 25, 14, 0),
            },
        }
        out, n = mergeLiveOptPxMap(daily, optSnap, asof=date(2026, 8, 25))
        self.assertEqual(n, 1)
        self.assertEqual(out["10012253.SH"], 0.1553)
        self.assertEqual(out["10010971.SH"], 0.12)


class FmtLastPxTest(unittest.TestCase):
    def testAlwaysFourDecimals(self):
        from rt_monitor.services.opt_t_board import _fmt_opt_last_px

        self.assertEqual(_fmt_opt_last_px(0.0167), '0.0167')
        self.assertEqual(_fmt_opt_last_px(0.03), '0.0300')
        self.assertEqual(_fmt_opt_last_px(0.235), '0.2350')
        self.assertEqual(_fmt_opt_last_px(None), '—')


class FmtIvPctTest(unittest.TestCase):
    def testThreeDigitNoDecimal(self):
        from rt_monitor.services.opt_t_board import _fmt_opt_iv_pct

        self.assertEqual(_fmt_opt_iv_pct(175.7), '176')
        self.assertEqual(_fmt_opt_iv_pct(100.0), '100')
        self.assertEqual(_fmt_opt_iv_pct(99.9), '99.9')
        self.assertEqual(_fmt_opt_iv_pct(35.11), '35.1')
        self.assertEqual(_fmt_opt_iv_pct(None), '—')


class SnapshotFetchPolicyTest(unittest.TestCase):
    def testTimeoutAllowsBusySnapshot(self):
        self.assertGreaterEqual(DAQMT_QUOTE_FETCH_TIMEOUT_SEC, 1.0)
        self.assertLessEqual(DAQMT_QUOTE_DOWN_COOLDOWN_SEC, 3.0)
        self.assertGreaterEqual(DAQMT_QUOTE_MEMO_SEC, 0.4)
        self.assertLessEqual(DAQMT_QUOTE_MEMO_SEC, 0.8)


class DisplayRowFilterTest(unittest.TestCase):
    def testAllListedContractsKept(self):
        self.assertTrue(
            _optTBoardRowIsDisplay(
                {'name': '510500ETF购8月7.5', 'per_unit': 10000, 'call_put': 'C'},
                10000,
            )
        )
        self.assertTrue(
            _optTBoardRowIsDisplay(
                {'name': '510500ETF购8月7.5A', 'per_unit': 10000, 'call_put': 'C'},
                10000,
            )
        )
        self.assertTrue(
            _optTBoardRowIsDisplay(
                {'name': '510500ETF购8月7.5', 'per_unit': 10075, 'call_put': 'P'},
                10000,
            )
        )

    def testDisplayBucketsKeepAdjSeparate(self):
        from rt_monitor.services.opt_t_board import (
            _fmt_strike_display,
            _optTBoardFillDisplayBuckets,
            _optTBoardNextSameClassIdx,
            _optTBoardSortDisplayKeys,
        )

        rows = [
            {
                'ts_code': '10000001.SH',
                'call_put': 'C',
                'exercise_price': 1.5,
                's_month': '202610',
                'maturity_date': date(2026, 10, 28),
                'name': '科创50ETF购10月1.50',
                'per_unit': 10000,
            },
            {
                'ts_code': '10000002.SH',
                'call_put': 'C',
                'exercise_price': 1.5,
                's_month': '202610',
                'maturity_date': date(2026, 10, 28),
                'name': '科创50ETF购10月1.50A',
                'per_unit': 10075,
            },
            {
                'ts_code': '10000003.SH',
                'call_put': 'P',
                'exercise_price': 1.5,
                's_month': '202610',
                'maturity_date': date(2026, 10, 28),
                'name': '科创50ETF沽10月1.50A',
                'per_unit': 10075,
            },
        ]
        groups = _optTBoardFillDisplayBuckets(rows)
        g = groups[('202610', '2026-10-28')]
        keys = _optTBoardSortDisplayKeys(list(set(g['calls']) | set(g['puts'])))
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0][2], 0)
        self.assertEqual(keys[1][2], 1)
        self.assertEqual(g['calls'][keys[0]]['ts_code'], '10000001.SH')
        self.assertEqual(g['calls'][keys[1]]['ts_code'], '10000002.SH')
        self.assertEqual(g['puts'][keys[1]]['ts_code'], '10000003.SH')
        self.assertEqual(_fmt_strike_display(1.5, isAdj=True), '1.500A')
        self.assertEqual(_optTBoardNextSameClassIdx(keys, 0, 1), None)
        self.assertEqual(_optTBoardNextSameClassIdx(keys, 1, -1), None)

    def testStdBlockThenAdjByStrike(self):
        from rt_monitor.services.opt_t_board import _optTBoardSortDisplayKeys

        keys = [
            (1.60, 10000.0, 0),
            (1.40, 10000.0, 1),
            (1.50, 10000.0, 0),
            (1.70, 10000.0, 1),
        ]
        out = _optTBoardSortDisplayKeys(keys)
        self.assertEqual(
            out,
            [
                (1.50, 10000.0, 0),
                (1.60, 10000.0, 0),
                (1.40, 10000.0, 1),
                (1.70, 10000.0, 1),
            ],
        )

    def testRankTop3ByAdjGroup(self):
        from rt_monitor.services.opt_t_board import _opt_t_rank_top3_indices_by_group

        values = [100.0, 200.0, 80.0, 50.0, 70.0, 10.0]
        groups = [0, 1, 0, 1, 0, 0]
        out = _opt_t_rank_top3_indices_by_group(values, groups)
        self.assertEqual(out[0], 1)
        self.assertEqual(out[2], 2)
        self.assertEqual(out[4], 3)
        self.assertEqual(out[1], 1)
        self.assertEqual(out[3], 2)
        self.assertNotIn(5, out)

    def testAtmByAdjIndependent(self):
        from rt_monitor.services.opt_t_board import _opt_t_board_pick_atm_by_adj

        keys = [
            (7.00, 10000.0, 0),
            (7.50, 10000.0, 0),
            (8.00, 10000.0, 0),
            (6.00, 10075.0, 1),
            (6.50, 10075.0, 1),
        ]
        atm = _opt_t_board_pick_atm_by_adj(keys, 7.48)
        self.assertAlmostEqual(atm[0], 7.50)
        self.assertAlmostEqual(atm[1], 6.50)

    def testUndlMatchStripsMarket(self):
        self.assertTrue(_optMetaUndlMatches('510500.SH', '510500'))
        self.assertTrue(_optMetaUndlMatches('510500', '510500'))
        self.assertFalse(_optMetaUndlMatches('588000.SH', '510500'))


class DelistSqlTest(unittest.TestCase):
    def testKeepExpiryDay(self):
        import inspect
        from rt_monitor.services import opt_t_board as mod

        src = inspect.getsource(mod._query_opt_t_board_payload)
        self.assertIn('delist_date >= CURDATE()', src)
        self.assertNotIn('delist_date > CURDATE()', src)


class StubEtfSpotTest(unittest.TestCase):
    def testSkipEmptyShellSpot(self):
        from rt_monitor.services.opt_t_board import isStubDaqmtEtfTick

        stub = {
            "last": 1.691,
            "open": 1.691,
            "high": 1.691,
            "low": 1.691,
            "vol": 0,
            "t": _ms(2026, 8, 25, 15, 0),
        }
        self.assertTrue(isStubDaqmtEtfTick(stub))
        quoteMap = {"10010971.SH": {"close": 0.10, "vol": 1, "oi": 2}}
        snap = {
            "etf": {"588000.SH": stub},
            "opt": {
                "10010971.SHO": {
                    "last": 0.05,
                    "vol": 10,
                    "oi": 99,
                    "t": _ms(2026, 8, 25, 10, 0),
                }
            },
        }
        _out, spot, n, src = overlayDaqmtQuotes(
            quoteMap, 1.68, "588000", snap, asof=date(2026, 8, 25)
        )
        self.assertEqual(n, 1)
        self.assertEqual(src, "daqmt")
        self.assertEqual(spot, 1.68)

    def testKeepRealSessionSpot(self):
        from rt_monitor.services.opt_t_board import isStubDaqmtEtfTick

        packed = {
            "last": 1.693,
            "open": 1.666,
            "high": 1.714,
            "low": 1.651,
            "vol": 3.3e9,
        }
        self.assertFalse(isStubDaqmtEtfTick(packed))


class MergeQmtChainGapsTest(unittest.TestCase):
    def testMetaToRowCallAndPut(self):
        row = qmtMetaToOptBasicRow(
            "10012153.SHO",
            {"k": 1.7, "exp": "2026-08-26", "sm": "202608", "cp": "C", "unit": 10000, "undl": "588000"},
        )
        self.assertEqual(row["ts_code"], "10012153.SH")
        self.assertEqual(row["call_put"], "C")
        self.assertEqual(row["exercise_price"], 1.7)
        self.assertEqual(row["s_month"], "202608")
        self.assertEqual(row["maturity_date"], date(2026, 8, 26))
        put = qmtMetaToOptBasicRow(
            "90007784.SZO",
            {"k": 2.5, "exp": "2026-09-23", "sm": "", "cp": "P", "unit": 10000, "undl": "159915"},
        )
        self.assertEqual(put["ts_code"], "90007784.SZ")
        self.assertEqual(put["call_put"], "P")
        self.assertEqual(put["s_month"], "202609")

    def testMergeAddsMissingStrike(self):
        raw = [
            {
                "ts_code": "10010971.SH",
                "call_put": "C",
                "exercise_price": 1.6,
                "s_month": "202608",
                "maturity_date": date(2026, 8, 26),
            }
        ]
        snap = {
            "optMeta": {
                "10019999.SHO": {
                    "k": 1.85,
                    "exp": "2026-08-26",
                    "sm": "202608",
                    "cp": "C",
                    "unit": 10000,
                    "undl": "588000",
                }
            }
        }
        out, n = mergeQmtChainGaps(raw, {"10010971.SH"}, snap, "588000", 10000)
        self.assertEqual(n, 1)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[-1]["ts_code"], "10019999.SH")
        self.assertEqual(out[-1]["exercise_price"], 1.85)

    def testMergeAddsWhenUndlHasExchangeSuffix(self):
        snap = {
            "optMeta": {
                "10019999.SHO": {
                    "k": 1.85,
                    "exp": "2026-10-28",
                    "sm": "202610",
                    "cp": "C",
                    "unit": 10000,
                    "undl": "588000.SH",
                }
            }
        }
        out, n = mergeQmtChainGaps([], set(), snap, "588000", 10000)
        self.assertEqual(n, 1)
        self.assertEqual(out[0]["ts_code"], "10019999.SH")
        self.assertEqual(out[0]["s_month"], "202610")

    def testSkipListedIncludingA(self):
        snap = {
            "optMeta": {
                "10018888.SHO": {
                    "k": 1.9,
                    "exp": "2026-08-26",
                    "sm": "202608",
                    "cp": "C",
                    "unit": 10000,
                    "undl": "588000",
                }
            }
        }
        out, n = mergeQmtChainGaps([], {"10018888.SH"}, snap, "588000", 10000)
        self.assertEqual(n, 0)
        self.assertEqual(out, [])

    def testOptBasicAlreadyListedSkipsQmtDuplicate(self):
        raw = [
            {
                "ts_code": "10012371.SH",
                "call_put": "C",
                "exercise_price": 1.9,
                "s_month": "202610",
                "maturity_date": date(2026, 10, 28),
            }
        ]
        snap = {
            "optMeta": {
                "10012371.SHO": {
                    "k": 1.9,
                    "exp": "2026-10-28",
                    "sm": "202610",
                    "cp": "C",
                    "unit": 10000,
                    "undl": "588000.SH",
                }
            }
        }
        out, n = mergeQmtChainGaps(raw, {"10012371.SH"}, snap, "588000", 10000)
        self.assertEqual(n, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ts_code"], "10012371.SH")
        self.assertIs(out[0], raw[0])

    def testSkipWrongUndlKeepOtherUnit(self):
        snap = {
            "optMeta": {
                "10019999.SHO": {
                    "k": 1.85,
                    "exp": "2026-08-26",
                    "sm": "202608",
                    "cp": "C",
                    "unit": 10000,
                    "undl": "510300",
                },
                "10019998.SHO": {
                    "k": 1.85,
                    "exp": "2026-08-26",
                    "sm": "202608",
                    "cp": "P",
                    "unit": 100,
                    "undl": "588000",
                },
            }
        }
        out, n = mergeQmtChainGaps([], set(), snap, "588000", 10000)
        self.assertEqual(n, 1)
        self.assertEqual(out[0]["ts_code"], "10019998.SH")
        self.assertEqual(out[0]["per_unit"], 100)

    def testCffexNoMerge(self):
        snap = {
            "optMeta": {
                "IO2509-C-4000.IF": {
                    "k": 4000,
                    "exp": "2026-09-18",
                    "sm": "202609",
                    "cp": "C",
                    "unit": 100,
                    "undl": "000300",
                }
            }
        }
        out, n = mergeQmtChainGaps([], set(), snap, "000300", 100)
        self.assertEqual(n, 0)
        self.assertEqual(out, [])


class BidAskPxAndSellerTest(unittest.TestCase):
    def testSellBuyPriority(self):
        from rt_monitor.services.opt_t_board import _optQuoteBuyPx, _optQuoteSellPx

        q = {"bid1": 0.10, "ask1": 0.12, "last": 0.11, "close": 0.09}
        self.assertEqual(_optQuoteSellPx(q), 0.10)
        self.assertEqual(_optQuoteBuyPx(q), 0.12)
        self.assertEqual(_optQuoteSellPx({"last": 0.11, "close": 0.09}), 0.11)
        self.assertEqual(_optQuoteBuyPx({"close": 0.09}), 0.09)
        self.assertIsNone(_optQuoteSellPx({}))
        self.assertEqual(_optQuoteBuyPx({"bid1": 0, "ask1": -1}), None)

    def testLastPxIgnoresBook(self):
        from rt_monitor.services.opt_t_board import _optQuoteLastPx

        q = {"bid1": 0.10, "ask1": 0.12, "last": 0.11, "close": 0.09}
        self.assertEqual(_optQuoteLastPx(q), 0.11)
        self.assertEqual(_optQuoteLastPx({"bid1": 0.10, "close": 0.09}), 0.09)
        self.assertEqual(
            _optQuoteLastPx({"daily_close": 1.3421, "close": 1.29, "last": None}),
            1.3421,
        )
        self.assertIsNone(_optQuoteLastPx({"bid1": 0.10, "ask1": 0.12}))

    def testScaleAbsurdOnlyRejectsOrderOfMagnitude(self):
        from rt_monitor.services.opt_t_board import _optLastScaleAbsurd

        # 略出买卖一：正常
        self.assertFalse(_optLastScaleAbsurd(0.184, 0.1845, 0.1854))
        self.assertFalse(_optLastScaleAbsurd(1.4838, 1.453, 1.4598))
        # ×10 级错价
        self.assertTrue(_optLastScaleAbsurd(0.12, 1.28, 1.30))
        self.assertTrue(_optLastScaleAbsurd(12.9, 1.28, 1.30))

    def testOutsideBookLastStillTradeLiveWithVol(self):
        """成交后 last 略出买卖一：仍用源端最新+成交量，不得回退昨收/清零量。"""
        quoteMap = {
            "10012279.SH": {
                "close": 1.5150,
                "daily_close": 1.5150,
                "vol": 0,
                "oi": 100,
                "trade_date": date(2026, 9, 9),
            }
        }
        snap = {
            "etf": {"510500.SH": {"last": 7.72}},
            "opt": {
                "10012279.SHO": {
                    "last": 1.4838,
                    "vol": 21,
                    "oi": 200,
                    "bid1": 1.453,
                    "ask1": 1.4598,
                    "fromTdx": True,
                    "t": _ms(2026, 9, 10, 10, 0),
                }
            },
            "optMeta": {
                "10012279.SHO": {
                    "undl": "510500.SH",
                    "k": 6.25,
                    "exp": "2026-09-23",
                    "cp": "C",
                    "unit": 10000,
                }
            },
        }
        out, _spot, _n, _src = overlayDaqmtQuotes(
            quoteMap, 7.5, "510500", snap, asof=date(2026, 9, 10)
        )
        from rt_monitor.services.opt_t_board import _optQuoteLastPx

        row = out["10012279.SH"]
        self.assertEqual(row.get("last"), 1.4838)
        self.assertEqual(_optQuoteLastPx(row), 1.4838)
        self.assertEqual(row.get("vol"), 21)
        self.assertTrue(row.get("trade_live"))
        self.assertEqual(row.get("daily_close"), 1.5150)

    def testBelowIntrinsicLastStillTradeLive(self):
        """深实值负时值仍是真实成交：有量 → 最新用 last，不用昨收。"""
        quoteMap = {
            "10012283.SH": {
                "close": 1.3421,
                "daily_close": 1.3421,
                "vol": 0,
                "oi": 100,
                "trade_date": date(2026, 9, 9),
            }
        }
        snap = {
            "etf": {"510500.SH": {"last": 7.72}},
            "opt": {
                "10012283.SHO": {
                    "last": 1.2925,
                    "vol": 12,
                    "oi": 200,
                    "bid1": 1.2832,
                    "ask1": 1.2958,
                    "fromTdx": True,
                    "t": _ms(2026, 9, 10, 10, 0),
                }
            },
            "optMeta": {
                "10012283.SHO": {
                    "undl": "510500.SH",
                    "k": 6.25,
                    "exp": "2027-03-24",
                    "cp": "C",
                    "unit": 10000,
                }
            },
        }
        out, spot, _n, _src = overlayDaqmtQuotes(
            quoteMap, 7.5, "510500", snap, asof=date(2026, 9, 10)
        )
        from rt_monitor.services.opt_t_board import _optQuoteLastPx

        row = out["10012283.SH"]
        self.assertEqual(spot, 7.72)
        self.assertEqual(row.get("last"), 1.2925)
        self.assertTrue(row.get("trade_live"))
        self.assertEqual(row.get("vol"), 12)
        self.assertEqual(_optQuoteLastPx(row), 1.2925)
        self.assertEqual(row.get("daily_close"), 1.3421)

    def testOverlayDropsAbsurdScaleLastKeepsDailyClose(self):
        quoteMap = {
            "10012283.SH": {
                "close": 1.3421,
                "daily_close": 1.3421,
                "vol": 0,
                "oi": 100,
                "trade_date": date(2026, 9, 9),
            }
        }
        snap = {
            "etf": {"510500.SH": {"last": 7.72}},
            "opt": {
                "10012283.SHO": {
                    "last": 12.9,
                    "vol": 10,
                    "oi": 200,
                    "bid1": 1.28,
                    "ask1": 1.30,
                    "fromTdx": True,
                    "t": _ms(2026, 9, 10, 10, 0),
                }
            },
            "optMeta": {
                "10012283.SHO": {
                    "undl": "510500.SH",
                    "k": 6.25,
                    "exp": "2027-03-24",
                    "cp": "C",
                    "unit": 10000,
                }
            },
        }
        out, spot, n, src = overlayDaqmtQuotes(
            quoteMap, 7.5, "510500", snap, asof=date(2026, 9, 10)
        )
        self.assertEqual(src, "tdx")
        self.assertEqual(n, 1)
        self.assertEqual(spot, 7.72)
        row = out["10012283.SH"]
        self.assertIsNone(row.get("last"))
        self.assertEqual(row.get("daily_close"), 1.3421)
        self.assertEqual(row.get("close"), 1.3421)
        self.assertEqual(row.get("vol"), 0)
        self.assertFalse(row.get("trade_live"))
        self.assertTrue(row.get("has_book"))

    def testOverlayKeepsSourceLastWhenVolLagZero(self):
        """TDX Volume 滞后为 0 时仍保留源端最新，不回退昨收。"""
        quoteMap = {
            "10010971.SH": {
                "close": 0.20,
                "daily_close": 0.20,
                "vol": 0,
                "oi": 10,
                "trade_date": date(2026, 9, 9),
            }
        }
        snap = {
            "etf": {"510050.SH": {"last": 3.0}},
            "opt": {
                "10010971.SHO": {
                    "last": 0.165,
                    "vol": 0,
                    "oi": 4040,
                    "bid1": 0.164,
                    "ask1": 0.166,
                    "fromTdx": True,
                    "t": _ms(2026, 9, 10, 10, 0),
                }
            },
            "optMeta": {
                "10010971.SHO": {
                    "undl": "510050.SH",
                    "k": 3.0,
                    "exp": "2026-09-24",
                    "cp": "C",
                    "unit": 10000,
                }
            },
        }
        out, _spot, n, src = overlayDaqmtQuotes(
            quoteMap, 3.0, "510050", snap, asof=date(2026, 9, 10)
        )
        self.assertEqual(src, "tdx")
        self.assertEqual(n, 1)
        row = out["10010971.SH"]
        self.assertEqual(row.get("last"), 0.165)
        self.assertEqual(row.get("close"), 0.165)
        self.assertEqual(row.get("vol"), 0)
        self.assertFalse(row.get("trade_live"))
        self.assertTrue(row.get("live"))

    def testBuyModeWhenTimeValNegUsesAskEverywhere(self):
        from rt_monitor.services.opt_t_board import _opt_t_board_seller_metrics

        # 深实值 Put：内在=5，bid=4.5→探针时值&lt;0；全套改用 ask=4.8
        m = _opt_t_board_seller_metrics(
            sellPx=4.5,
            buyPx=4.8,
            strike=10.0,
            spot=5.0,
            forward=5.0,
            callPut="P",
            daysToExpiry=30,
            rate=0.015,
        )
        self.assertEqual(m["action_mode"], "buy")
        self.assertEqual(m["action_px"], 4.8)
        self.assertEqual(m["eff_px"], "5.2000")  # 10-4.8
        self.assertEqual(m["time_val"], "-0.2000")  # 4.8-5
        self.assertEqual(m["disc_prem_pct"], "-4.00")  # (5-5.2)/5
        self.assertEqual(m["time_cls"], "opt-t-tv-neg")
        self.assertEqual(m["px_label"], "买价")

    def testCrossingBidNegAskPosUsesBid(self):
        """夹心区：买一略折、卖一已正时值 → 仍卖方口径用买一（2.850 典型）。"""
        from rt_monitor.services.opt_t_board import _opt_t_board_seller_metrics

        m = _opt_t_board_seller_metrics(
            sellPx=0.1701,
            buyPx=0.1744,
            strike=2.85,
            spot=3.021,
            forward=3.021,
            callPut="C",
            daysToExpiry=105,
            rate=0.015,
        )
        self.assertEqual(m["action_mode"], "sell")
        self.assertEqual(m["action_px"], 0.1701)
        self.assertEqual(m["time_val"], "-0.0009")
        self.assertEqual(m["eff_px"], "3.0201")

    def testSellModeWhenAskTimeValNonNeg(self):
        from rt_monitor.services.opt_t_board import _opt_t_board_seller_metrics

        m2 = _opt_t_board_seller_metrics(
            sellPx=0.20,
            buyPx=0.25,
            strike=10.0,
            spot=10.5,
            forward=10.5,
            callPut="P",
            daysToExpiry=30,
            rate=0.015,
        )
        self.assertEqual(m2["action_mode"], "sell")
        self.assertEqual(m2["action_px"], 0.20)
        self.assertEqual(m2["eff_px"], "9.8000")
        self.assertEqual(m2["time_val"], "0.2000")
        self.assertEqual(m2["disc_prem_pct"], "6.67")
        self.assertEqual(m2["px_label"], "卖价")
        # 时年化两位：(0.2/10)*365/30*100
        self.assertEqual(m2["tv_ann"], "24.33")

    def testExpiryDayStillSolvesIvAndSkipsTvAnn(self):
        """到期日 DTE=0 仍交易：IV/Δ 按 1/365 反解；时年化分母为 0 保持空。"""
        from rt_monitor.services.opt_t_board import _opt_t_board_seller_metrics

        m = _opt_t_board_seller_metrics(
            sellPx=0.020,
            strike=1.75,
            spot=1.76,
            forward=1.76,
            callPut="C",
            daysToExpiry=0,
            rate=0.015,
        )
        self.assertNotEqual(m.get("iv"), "—")
        self.assertNotEqual(m.get("delta"), "—")
        self.assertEqual(m.get("tv_ann"), "—")

        mNeg = _opt_t_board_seller_metrics(
            sellPx=0.020,
            strike=1.75,
            spot=1.76,
            forward=1.76,
            callPut="C",
            daysToExpiry=-1,
            rate=0.015,
        )
        self.assertEqual(mNeg.get("iv"), "—")


    def testCallDec2700MatchesBuyAskCaliber(self):
        """回归：510050 12月认购 2.700，页上应买价=ask，时值/有效价/折溢同口径。"""
        from rt_monitor.services.opt_t_board import _opt_t_board_seller_metrics

        m = _opt_t_board_seller_metrics(
            sellPx=0.2859,
            buyPx=0.2931,
            strike=2.7,
            spot=3.021,
            forward=3.021,
            callPut="C",
            daysToExpiry=105,
            rate=0.015,
        )
        self.assertEqual(m["action_mode"], "buy")
        self.assertEqual(m["action_px"], 0.2931)
        self.assertEqual(m["time_val"], "-0.0279")  # 0.2931-(3.021-2.7)
        self.assertEqual(m["eff_px"], "2.9931")
        self.assertEqual(m["disc_prem_pct"], "-0.92")
        # 权利金% = 0.2931/(3.021-0.2931)
        self.assertEqual(m["prem_pct"], "10.74")


class OverlayTdxBookTest(unittest.TestCase):
    def testTdxBookMarksLiveAndSource(self):
        quoteMap = {
            "10010971.SH": {
                "close": 0.10,
                "vol": 0,
                "oi": 2,
                "trade_date": date(2026, 9, 9),
            }
        }
        asof = date(2026, 9, 9)
        snap = {
            "opt": {
                "10010971.SHO": {
                    "last": 0.10,
                    "vol": 0,
                    "lastClose": 0.10,
                    "bid1": 0.099,
                    "ask1": 0.101,
                    "fromTdx": True,
                    "t": _ms(2026, 9, 9, 15, 5),
                }
            },
            "optMeta": {"10010971.SHO": {"undl": "510050"}},
            "optWall": "2026-09-09 15:05:01",
        }
        out, _spot, n, src = overlayDaqmtQuotes(
            quoteMap, 3.0, "510050", snap, asof=asof
        )
        self.assertEqual(src, "tdx")
        self.assertEqual(n, 1)
        self.assertTrue(out["10010971.SH"]["live"])
        self.assertTrue(out["10010971.SH"]["has_book"])
        self.assertTrue(out["10010971.SH"]["from_tdx"])
        self.assertEqual(out["10010971.SH"]["bid1"], 0.099)
        self.assertEqual(out["10010971.SH"]["ask1"], 0.101)

    def testLiveClockAfterHoursTdx(self):
        from rt_monitor.services.opt_t_board import _optTBoardLiveClockLabel

        label = _optTBoardLiveClockLabel(
            "tdx", {"optWall": "2026-09-09 15:05:01"}
        )
        self.assertEqual(label, "收盘盘口(TDX) 15:05:01")
        label2 = _optTBoardLiveClockLabel(
            "tdx", {"optWall": "2026-09-09 10:30:00"}
        )
        self.assertEqual(label2, "实时(TDX) 10:30:00")
        label3 = _optTBoardLiveClockLabel(
            "daqmt", {"optWall": "2026-09-09 10:30:00"}
        )
        self.assertEqual(label3, "实时 10:30:00")

    def testLiveClockSeparatesLocalObservationFromLastMarketEvent(self):
        from rt_monitor.services.opt_t_board import _optTBoardLiveClockLabel

        label = _optTBoardLiveClockLabel(
            "mixed",
            {
                "optWall": "2026-09-15T18:48:12",
                "etf": {"510050.SH": {"time": 1789457451001}},
                "opt": {
                    "10010971.SHO": {"time": 1789455602536.4102},
                },
                "optMeta": {"10010971.SHO": {"undl": "510050.SH"}},
            },
            "510050",
            asof=date(2026, 9, 15),
        )

        self.assertEqual(
            label,
            "本地观测 18:48:12 · 最后市场事件 15:30:51 · 混合·TDX补",
        )


@pytest.mark.parametrize(
    ('expiryDateDisplay', 'daysToExpiryLabel', 'expiryKind', 'expectedLabel'),
    [
        ('2026-09-23', '剩8天', 'near', '当月'),
        ('2026-10-28', '剩43天', 'next', '下月'),
        ('2026-12-23', '剩99天', 'quarter', '当季'),
        ('2027-03-24', '剩190天', 'next_quarter', '下季'),
    ],
)

def test_opt_t_board_expiry_meta_appends_etf_tenor_label(
    expiryDateDisplay: str,
    daysToExpiryLabel: str,
    expiryKind: str,
    expectedLabel: str,
) -> None:
    """ETF 四个期限桶的段头保留原日期、DTE，并追加展示期限。"""
    expiryMeta = _opt_t_board_format_expiry_meta(
        expiryDateDisplay, daysToExpiryLabel, expiryKind
    )

    assert expiryMeta == f'{expiryDateDisplay} · {daysToExpiryLabel} · {expectedLabel}'


@pytest.mark.parametrize(
    'expiryKind',
    ('next_next', 'next_next_quarter', 'unknown'),
)
def test_opt_t_board_expiry_meta_preserves_unrecognized_tenor_without_empty_separator(
    expiryKind: str,
) -> None:
    """未知/CFFEX 扩展期限不改原段头，也不会留下空分隔符。"""
    expiryMeta = _opt_t_board_format_expiry_meta(
        '2026-12-18', '剩94天', expiryKind
    )

    assert expiryMeta == '2026-12-18 · 剩94天'


def test_opt_t_board_obligation_selector_only_marks_highest_time_annualization() -> None:
    """选择器只用 ☆ 陈述同 K 的时年化最高，不再给主观期限打 ★。"""
    def row(
        strike: float,
        *,
        callTv: str = '—',
        callDelta: str = '—',
        callEff: str = '—',
        putTv: str = '—',
        putDelta: str = '—',
        putEff: str = '—',
    ) -> dict:
        return {
            'strike': strike,
            'strike_display': f'{strike:.2f}',
            'is_adj': False,
            'is_atm': strike == 100.0,
            'call_tv_ann': callTv,
            'call_delta': callDelta,
            'call_eff_px': callEff,
            'call_strategy_ready': callTv != '—',
            'put_tv_ann': putTv,
            'put_delta': putDelta,
            'put_eff_px': putEff,
            'put_strategy_ready': putTv != '—',
        }

    sections = [
        {
            'expiry_kind': kind,
            'rows': [
                row(95.0, putTv=putTv, putDelta=putDelta, putEff=putEff),
                row(100.0),
                row(105.0, callTv=callTv, callDelta=callDelta, callEff=callEff),
            ],
        }
        for kind, callTv, callDelta, callEff, putTv, putDelta, putEff in (
            ('near', '30.00', '0.28', '106.0000', '30.00', '-0.28', '94.0000'),
            ('next', '40.00', '0.30', '105.5000', '25.00', '-0.28', '93.0000'),
            ('quarter', '50.00', '0.50', '104.0000', '35.00', '-0.50', '95.0000'),
            ('next_quarter', '35.00', '0.27', '107.0000', '34.00', '-0.30', '92.0000'),
        )
    ]

    selector = _opt_t_board_build_obligation_selector(sections)
    assert [item['label'] for item in selector['columns']] == [
        '当月（年/Δ/有效价 ┊ Per 1y/C/A）', '下月（年/Δ/有效价 ┊ Per 1y/C/A）',
        '当季（年/Δ/有效价 ┊ Per 1y/C/A）', '下季（年/Δ/有效价 ┊ Per 1y/C/A）',
    ]
    assert [item['head_expiry'] for item in selector['columns']] == ['当月', '下月', '当季', '下季']
    assert all(item['head_left'] == '年/Δ/有效价' for item in selector['columns'])
    assert all(item['head_right'] == 'Per 1y/C/A' for item in selector['columns'])

    callOtm1 = next(item for item in selector['call']['rows'] if item['label'] == '虚一')
    assert [cell['yield_star'] for cell in callOtm1['cells']] == [False, False, True, False]
    assert all('candidate_star' not in cell for cell in callOtm1['cells'])

    putOtm1 = next(item for item in selector['put']['rows'] if item['label'] == '虚一')
    assert [cell['yield_star'] for cell in putOtm1['cells']] == [False, False, True, False]
    assert all('candidate_star' not in cell for cell in putOtm1['cells'])

    # 原 T 表同一行权价的左右标记与选择器同步。
    assert sections[2]['rows'][2]['call_selector_yield_star'] is True
    assert all(
        f'{side}_selector_candidate_star' not in row
        for section in sections
        for row in section['rows']
        for side in ('call', 'put')
    )


def test_opt_t_board_obligation_selector_displays_delta_without_candidate_gate() -> None:
    """Delta 完整展示，不承担期限候选筛选职责。"""
    def row(strike: float, *, tv: str = '—', delta: str = '—', eff: str = '—') -> dict:
        return {
            'strike': strike,
            'strike_display': f'{strike:.2f}',
            'is_adj': False,
            'is_atm': strike == 100.0,
            'call_tv_ann': tv if strike == 105.0 else '—',
            'call_delta': delta if strike == 105.0 else '—',
            'call_eff_px': eff if strike == 105.0 else '—',
            'call_strategy_ready': strike == 105.0,
            'put_tv_ann': '—',
            'put_delta': '—',
            'put_eff_px': '—',
            'put_strategy_ready': False,
        }

    sections = [
        {
            'expiry_kind': kind,
            'rows': [row(95.0), row(100.0), row(105.0, tv=tv, delta=delta, eff=eff)],
        }
        for kind, tv, delta, eff in (
            ('near', '30.00', '0.28', '106.0000'),
            ('next', '40.00', '0.50', '108.0000'),
            ('quarter', '50.00', '0.10', '107.0000'),
            ('next_quarter', '35.00', '0.27', '105.0000'),
        )
    ]

    selector = _opt_t_board_build_obligation_selector(sections)
    callOtm1 = next(item for item in selector['call']['rows'] if item['label'] == '虚一')
    assert [cell['delta'] for cell in callOtm1['cells']] == ['0.28', '0.50', '0.10', '0.27']


def test_opt_t_board_obligation_selector_columns_include_dynamic_days_to_expiry() -> None:
    """期限列显示服务实时计算的剩余自然日。"""
    sections = [
        {
            'expiry_kind': kind,
            'selector_days_to_expiry': days,
            'rows': [],
        }
        for kind, days in (
            ('near', 6), ('next', 41), ('quarter', 97), ('next_quarter', 189),
        )
    ]

    selector = _opt_t_board_build_obligation_selector(sections)

    assert [item['label'] for item in selector['columns']] == [
        '当月（6天）（年/Δ/有效价 ┊ Per 1y/C/A）',
        '下月（41天）（年/Δ/有效价 ┊ Per 1y/C/A）',
        '当季（97天）（年/Δ/有效价 ┊ Per 1y/C/A）',
        '下季（189天）（年/Δ/有效价 ┊ Per 1y/C/A）',
    ]
    assert [item['head_expiry'] for item in selector['columns']] == [
        '当月（6天）', '下月（41天）', '当季（97天）', '下季（189天）',
    ]


def test_opt_t_board_obligation_selector_reuses_moneyness_and_per_display() -> None:
    """左列沿用 T 型虚实%字段；三列分位直接沿用 T 表算好的文本/色阶/title，不再二次解析。"""
    def row(strike: float, *, atm: bool = False, ready: bool = False) -> dict:
        return {
            'strike': strike,
            'strike_display': f'{strike:.2f}',
            'moneyness_pct': '5.00' if strike == 105.0 else '—',
            'moneyness_cls': 'opt-t-mny-above' if strike == 105.0 else '',
            'is_adj': False,
            'is_atm': atm,
            'call_tv_ann': '20.00' if ready else '—',
            'call_delta': '0.30' if ready else '—',
            'call_eff_px': '106.0000' if ready else '—',
            # 展示格式为 0~100 一位小数，与 IV 看板同精度
            'call_per1y': '75.4' if ready else '—',
            'call_per1y_cls': 'opt-t-ivr-hi' if ready else '',
            'call_per1y_title': '近252日：分位 75（n=252）',
            'call_per1y_raw': 75.4 if ready else None,
            'call_perc': '62.1' if ready else '—',
            'call_perc_cls': 'opt-t-ivr-mid' if ready else '',
            'call_perc_title': '锚点 2024-09-24 起：分位 62（n=180）',
            'call_pera': '41.8' if ready else '—',
            'call_pera_cls': 'opt-t-ivr-lo' if ready else '',
            'call_pera_title': '全样本：分位 41（n=1200）',
            'call_strategy_ready': ready,
            'put_tv_ann': '12.00' if strike == 95.0 else '—',
            'put_delta': '-0.30' if strike == 95.0 else '—',
            'put_eff_px': '94.0000' if strike == 95.0 else '—',
            'put_strategy_ready': strike == 95.0,
        }

    sections = [{
        'expiry_kind': 'near',
        'rows': [row(95.0), row(100.0, atm=True), row(105.0, ready=True)],
    }]

    selector = _opt_t_board_build_obligation_selector(sections)
    callOtm1 = next(item for item in selector['call']['rows'] if item['label'] == '虚一')

    assert callOtm1['moneyness_pct'] == '5.00'
    assert callOtm1['moneyness_cls'] == 'opt-t-mny-above'
    assert callOtm1['bucket_cls'] == 'opt-t-selector-bucket-otm'
    cell = callOtm1['cells'][0]
    assert (cell['per1y'], cell['perc'], cell['pera']) == ('75.4', '62.1', '41.8')
    assert (cell['per1y_cls'], cell['perc_cls'], cell['pera_cls']) == (
        'opt-t-ivr-hi', 'opt-t-ivr-mid', 'opt-t-ivr-lo',
    )
    assert cell['perc_title'] == '锚点 2024-09-24 起：分位 62（n=180）'
    # 缺分位的 Put 侧三列一律占位，不泄漏 None
    putOtm1 = next(item for item in selector['put']['rows'] if item['label'] == '虚一')
    putCell = putOtm1['cells'][0]
    assert (putCell['per1y'], putCell['perc'], putCell['pera']) == ('—', '—', '—')


def test_opt_t_board_obligation_selector_does_not_star_percentile_columns() -> None:
    """分位列不标星：远月分位系统性偏高，四期限里的「最高」几乎恒为下季，无区分度。

    2026-09-20 实测 call/put 两侧 8 行的 ☆ 全部落在下季列，用户据此决定移除。
    ☆ 的唯一含义回到时年化最高。
    """
    def row(strike: float, *, atm: bool = False, per1y: float | None = None) -> dict:
        ready = strike == 105.0
        return {
            'strike': strike,
            'strike_display': f'{strike:.2f}',
            'is_adj': False,
            'is_atm': atm,
            'call_tv_ann': '20.00' if ready else '—',
            'call_delta': '0.30' if ready else '—',
            'call_eff_px': '106.0000' if ready else '—',
            'call_per1y': '—' if not ready or per1y is None else f'{per1y:.1f}',
            'call_per1y_raw': per1y if ready else None,
            'call_strategy_ready': ready,
            'put_tv_ann': '—',
            'put_delta': '—',
            'put_eff_px': '—',
            'put_strategy_ready': False,
        }

    sections = [
        {
            'expiry_kind': kind,
            'rows': [row(95.0), row(100.0, atm=True), row(105.0, per1y=per1y)],
        }
        for kind, per1y in (
            ('near', 40.0), ('next', 68.0),
            ('quarter', 65.0), ('next_quarter', 99.0),
        )
    ]

    selector = _opt_t_board_build_obligation_selector(sections)
    callOtm1 = next(item for item in selector['call']['rows'] if item['label'] == '虚一')

    # 三列一个都不带 *_max 标记字段，模板也就无从渲染星标
    for cell in callOtm1['cells']:
        assert not any(k.endswith('_max') for k in cell), sorted(cell)
    # 时年化的 ☆ 不受影响，仍按各档时年化独立判定
    assert any('yield_star' in cell for cell in callOtm1['cells'])
    # 分位数值本身照常展示
    assert [cell['per1y'] for cell in callOtm1['cells']] == ['40.0', '68.0', '65.0', '99.0']


def test_opt_t_board_selector_quote_uses_three_minute_bid_then_last_close() -> None:
    """选择器盘中只用三分钟内的买一；其余时间按最新价、收盘价估算。"""
    quote = {'bid1': 0.123, 'ask1': 0.124, 'last': 0.125, 'close': 0.120, 'book_live': True}

    assert _opt_t_board_selector_sell_price(
        quote, bookAgeSec=180.0, inMarketSession=True
    ) == (0.123, 'bid1')
    assert _opt_t_board_selector_sell_price(
        quote, bookAgeSec=180.1, inMarketSession=True
    ) == (0.125, 'last')
    assert _opt_t_board_selector_sell_price(
        quote, bookAgeSec=1.0, inMarketSession=False
    ) == (0.125, 'last')
    assert _opt_t_board_selector_sell_price(
        {'bid1': 0.123, 'close': 0.120, 'book_live': True},
        bookAgeSec=181.0,
        inMarketSession=True,
    ) == (0.120, 'close')


def test_opt_t_board_obligation_selector_aligns_standard_strikes_and_marks_missing_contracts() -> None:
    """有入选的档仍按标准 K 对齐；同档其他期限缺合约显示缺档。四期限都空的档不出行。"""
    def row(strike: float, *, callReady: bool = False) -> dict:
        return {
            'strike': strike,
            'strike_display': f'{strike:.2f}',
            'is_adj': False,
            'is_atm': strike == 100.0,
            'call_tv_ann': '20.00' if callReady else '—',
            'call_delta': '0.50' if callReady else '—',
            'call_eff_px': '106.0000' if callReady else '—',
            'call_strategy_ready': callReady,
            'put_tv_ann': '—',
            'put_delta': '—',
            'put_eff_px': '—',
            'put_strategy_ready': False,
        }

    standardStrikes = [75.0, 80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0, 125.0]
    sections = [
        {'expiry_kind': 'near', 'rows': [row(strike, callReady=strike == 105.0) for strike in standardStrikes]},
        {'expiry_kind': 'next', 'rows': [row(strike) for strike in standardStrikes if strike != 105.0]},
    ]

    selector = _opt_t_board_build_obligation_selector(sections)
    assert selector['put']['rows'] == []
    assert [(item['label'], item['strike_display']) for item in selector['call']['rows']] == [
        ('虚一', '105.00'),
    ]
    callOtm1 = next(item for item in selector['call']['rows'] if item['label'] == '虚一')
    assert callOtm1['cells'][0]['status'] == 'ready'
    assert callOtm1['cells'][1]['status'] == 'missing'


def test_opt_t_board_selector_fragment_keeps_selector_above_t_sections_and_sync_marks() -> None:
    """选择器不扩表列，且 Call/Put 标记必须分别落在行权价的左/右。"""
    root = Path(__file__).resolve().parents[1]
    body = (root / 'templates' / 'rt_monitor_opt_t_board_body.html').read_text(
        encoding='utf-8'
    )
    page = (root / 'templates' / 'rt_monitor_opt_t_board.html').read_text(
        encoding='utf-8'
    )

    assert body.index('class="opt-t-selector"') < body.index('{% for sec in opt_t_sections %}')
    assert 'data-opt-selector-tab="put"' in body
    assert 'data-opt-selector-tab="call"' in body
    assert 'opt-t-strike-marker' not in body
    assert 'opt-t-cell-strike-marked' not in body
    assert 'opt-t-strike-marker' not in page
    assert '<span>{{ row.strike_display }}</span>' in body
    # Jinja 缺键时 `is not none` 仍会进分支，|int 直接 500；必须先 is defined
    assert 'row.call_oi_n is defined and row.call_oi_n is not none' in body
    selectorFragment = body.split('{% for sec in opt_t_sections %}', 1)[0]
    assert '义务仓优选' in selectorFragment
    assert '档位 / K / 虚实%' in selectorFragment
    assert 'selector_row.moneyness_pct' in selectorFragment
    assert '<span class="opt-t-selector-strike-layout">' in selectorFragment
    assert '<span class="opt-t-selector-strike-label">{{ selector_row.label }}</span>' in selectorFragment
    assert '<span class="opt-t-selector-strike-k">（{{ selector_row.strike_display }}）</span>' in selectorFragment
    assert '<thead>\n                        <tr>\n' in selectorFragment
    assert (
        '{% for selector_row in tab.rows %}\n'
        '                        <tr class="opt-t-selector-row {{ selector_row.bucket_cls }}">'
    ) in selectorFragment
    assert 'selector_row.moneyness_pct }}%' not in selectorFragment
    assert '>{{ cell.per1y }}</span>' in selectorFragment
    assert '>{{ cell.perc }}</span>' in selectorFragment
    assert '>{{ cell.pera }}</span>' in selectorFragment
    assert 'opt-t-selector-metrics-sep' in selectorFragment
    assert 'col.head_expiry' in selectorFragment
    assert '{{ col.head_expiry }} {{ col.head_left }} ┊ {{ col.head_right }}' in selectorFragment
    assert 'class="opt-t-tenor-{{ col.kind }}"' in selectorFragment
    assert 'opt-t-tenor-{{ opt_t_obligation_selector.columns[loop.index0].kind }}' in selectorFragment
    assert 'opt-t-selector-th-expiry' not in selectorFragment
    assert 'opt-t-selector-th-left' not in selectorFragment
    assert 'opt-t-selector-per-lead' not in selectorFragment
    # 三列顺序固定为 Per 1y → Per C → Per A，且都排在有效价之后
    assert (
        selectorFragment.index('{{ cell.eff_px }}')
        < selectorFragment.index('{{ cell.per1y }}')
        < selectorFragment.index('{{ cell.perc }}')
        < selectorFragment.index('{{ cell.pera }}')
    )
    assert '>年 {{ cell.tv_ann }}' not in selectorFragment
    assert '>Δ {{ cell.delta }}' not in selectorFragment
    assert '>有效 {{ cell.eff_px }}' not in selectorFragment
    assert 'cell.ivr' not in selectorFragment
    # 分位列不标星（用户 2026-09-20 决定）：模板不得再引用任何 *_max 分位标记
    assert 'per1y_max' not in selectorFragment
    assert 'is-per1y-max' not in page
    assert 'K {{ cell.strike_display }}' not in selectorFragment
    assert 'opt-t-selector-k' not in selectorFragment
    assert '缺档' not in selectorFragment
    assert '|Δ|={{ opt_t_obligation_selector.delta_band }}' not in body
    assert '★' not in selectorFragment
    assert '盘中买一（3 分钟内）；其余时段按最新价→收盘价估算' in selectorFragment
    assert 'opt-t-selector-note' not in selectorFragment
    assert '有效价、Delta 与量仓用于自主取舍' not in selectorFragment
    assert '行情不足' not in selectorFragment
    assert '待重算' not in page
    assert 'markObligationSelectorPending' not in page
    selectorCss = page.split('        .opt-t-selector {', 1)[1].split(
        '        .opt-t-section {', 1
    )[0]
    assert 'font-size: var(--opt-board-fs);' in selectorCss
    assert 'line-height: var(--opt-board-lh);' in selectorCss
    assert 'font-size: var(--opt-board-fs-th);' in selectorCss
    assert 'width: 12.2rem;' in selectorCss
    assert 'width: 13.6rem;' not in selectorCss
    assert 'width: 11.8em;' not in selectorCss
    assert 'width: 15em;' not in selectorCss
    assert 'font-size: 0.64rem;' not in selectorCss
    assert 'min-width: 1760px' not in selectorCss
    assert '.opt-t-selector-scroll { overflow-x: hidden; }' in selectorCss
    assert '.opt-t-selector-th {' in selectorCss
    thCss = selectorCss.split('.opt-t-selector-th {', 1)[1].split('.opt-t-selector-metrics {', 1)[0]
    assert 'white-space: nowrap;' in thCss
    assert 'text-align: center;' in thCss
    assert 'flex-direction' not in thCss
    assert 'opt-t-selector-th-metrics' not in body
    assert '.opt-t-selector-th-expiry' not in selectorCss
    assert '.opt-t-selector-th-left' not in selectorCss
    assert '.opt-t-selector-th-sep' not in selectorCss
    assert '.opt-t-selector-strike-layout { display: grid;' in selectorCss
    assert 'grid-template-columns: 2.8em 5.2em 3.5em;' in selectorCss
    assert 'justify-content: center;' in selectorCss
    assert '.opt-t-selector-row.opt-t-selector-bucket-atm > th.opt-t-selector-strike { background: #fff3e0; }' in selectorCss
    assert '.opt-t-selector-row.opt-t-selector-bucket-atm > td.opt-t-tenor-near { background: color-mix(' in selectorCss
    assert '#fde68a 72%' in selectorCss
    assert 'inset 0 2px 0 0 rgba(217, 119, 6, 0.62)' in selectorCss
    assert 'thead th.opt-t-tenor-near { background: #dce8f4; }' in selectorCss
    assert 'thead th.opt-t-tenor-next { background: #d8eee4; }' in selectorCss
    assert 'thead th.opt-t-tenor-quarter { background: #eee6d6; }' in selectorCss
    assert 'thead th.opt-t-tenor-next_quarter { background: #e6deee; }' in selectorCss
    assert '.opt-t-selector-metrics .opt-t-ivr-mid { color: #1d4ed8; font-weight: 800; background: #eff6ff;' in selectorCss
    assert '.opt-t-selector-metrics .opt-t-ivr-hi { color: #1e3a8a; font-weight: 800; background: #dbeafe;' in selectorCss
    rightCss = selectorCss.split('.opt-t-selector-metrics-right {', 1)[1].split('.opt-t-selector-metrics-right > span', 1)[0]
    assert 'display: grid;' in rightCss
    assert 'grid-template-columns: 1fr 1fr 1fr;' in rightCss
    assert 'width: 100%;' in rightCss
    assert 'column-gap: 2px;' in rightCss
    assert 'padding-right: 14px;' in rightCss
    assert 'border-top:' not in rightCss
    assert 'justify-content: space-between;' not in rightCss
    metricsBlock = selectorCss.split('.opt-t-selector-metrics {', 1)[1].split('.opt-t-selector-metrics-left', 1)[0]
    assert 'display: grid;' in metricsBlock
    assert 'grid-template-columns: max-content auto minmax(0, 1fr);' in metricsBlock
    assert 'grid-template-rows: auto auto;' not in metricsBlock
    assert 'font-size: 0.74rem;' not in metricsBlock
    assert 'grid-template-columns: calc(2.7em + 6px) calc(3.05em + 6px) calc(3.15em + 6px);' in selectorCss
    assert 'grid-template-columns: calc(2.9em + 6px) calc(3.25em + 6px) calc(3.4em + 6px);' in page
    assert 'grid-template-columns: 3.2em 4.6em 5.2em;' not in page
    assert 'display: none;' not in selectorCss.split('.opt-t-selector-metrics-sep {', 1)[1].split('}', 1)[0]
    assert 'width: 12.2rem;' in selectorCss
    assert 'flex: 0 0 auto;' in selectorCss
    assert 'flex: 1 1 0;' in selectorCss
    assert '.opt-t-selector-stars { position: absolute;' in selectorCss
    assert 'letter-spacing: -1px;' not in selectorCss.split('.opt-t-selector-stars {', 1)[1].split('.opt-t-selector-cell.is-empty', 1)[0]
    assert '.opt-t-cg-iv { width: 2.4%; }' in page
    assert '.opt-t-cg-tv { width: 4.0%; }' in page
    assert 'tr.opt-t-row-atm > td:not([class*="opt-t-rank-"])' in page
    assert '.opt-t-rank-call-vol-1 { background:' in page
    assert '.opt-t-cg-gamma { width: 2.5%; }' in page
    assert '.opt-t-cg-delta { width: 3.0%; }' in page
    assert '.opt-t-cg-per { width: 2.4%; }' in page


def testOptTBoardLayoutKeepsSideBySideOnDesktop():
    """桌面宽度必须左右并排：禁止 3360 断点把左栏写成 100% 独占整行。"""
    page = (
        Path(__file__).resolve().parents[1]
        / 'templates'
        / 'rt_monitor_opt_t_board.html'
    ).read_text(encoding='utf-8')
    layoutCss = page.split('.opt-t-layout {', 1)[1].split('.opt-t-main-col {', 1)[0]
    mainCss = page.split('.opt-t-main-col {', 1)[1].split('.opt-t-right-col {', 1)[0]
    rightCss = page.split('.opt-t-right-col {', 1)[1].split('.opt-sp-plan {', 1)[0]
    wrapCss = ''
    if '@media (max-width: 3360px)' in page:
        wrapCss = page.split('@media (max-width: 3360px)', 1)[1][:400]
    assert 'flex-wrap: nowrap' in layoutCss
    assert 'flex: 1 1 0%' in mainCss
    assert 'min-width: 40%;' in mainCss
    assert 'flex: 1 1 auto' not in mainCss
    assert 'flex: 1 1 100%' not in mainCss
    assert 'flex: 0 1 1560px' in rightCss
    assert 'max-width: 1560px' in rightCss
    assert 'flex: 0 1 520px' not in rightCss
    assert 'flex: 0 1 640px' not in rightCss
    assert 'flex: 0 1 960px' not in rightCss
    assert 'flex: 0 1 1080px' not in rightCss
    assert 'flex: 0 1 640px' not in rightCss
    assert 'min(1530px, 46%)' not in rightCss
    assert '@media (max-width: 3360px)' not in page
    assert 'flex: 1 1 100%' not in wrapCss
    ivScrollCss = page.split('.opt-iv-scroll {', 1)[1].split('.opt-iv-table {', 1)[0]
    ivTableCss = page.split('.opt-iv-table {', 1)[1].split('.opt-iv-cg-date {', 1)[0]
    assert 'overflow-x: hidden;' in ivScrollCss
    assert 'overflow-x: auto;' not in ivScrollCss
    assert 'width: 100%;' in ivTableCss
    assert 'width: 1530px;' not in ivTableCss


def testOptTBoardRightColumnRendersIvBeforeSellPut():
    """右侧栏须先展示 IV，再展示 Sell Put；读盘指引保持在二者之后。"""
    templatePath = Path(__file__).resolve().parents[1] / "templates" / "rt_monitor_opt_t_board.html"
    templateText = templatePath.read_text(encoding="utf-8")
    rightCol = templateText.split('<div class="opt-t-right-col">', 1)[1]

    ivIndex = rightCol.index('<div id="opt-iv-board-root">')
    sellPutIndex = rightCol.index("{% include 'rt_monitor_opt_sp_plan.html' %}")
    guideIndex = rightCol.index("{% include 'rt_monitor_opt_vol_guide.html' %}")

    assert ivIndex < sellPutIndex < guideIndex


@pytest.mark.parametrize(
    ('deltaText', 'shouldDisplay'),
    (
        ('0.09', False),
        ('0.10', True),
        ('0.90', True),
        ('0.91', False),
        ('-0.10', True),
        ('-0.90', True),
    ),
)
def test_opt_t_per_display_delta_range_is_inclusive_10_to_90(
    deltaText: str,
    shouldDisplay: bool,
) -> None:
    """三列分位仅隐藏 Delta 绝对值低于 0.10 或高于 0.90 的合约；门槛对三列一致。"""
    # 72 个样本（>MIN_HISTORY=60），当前值落在第 51 位 → 分位约 0.70
    vals = [0.10 + i * 0.001 for i in range(72)]
    dates = [date(2026, 1, 1) for _ in vals]
    windows = ivWindows.buildPerWindows(dates, vals, anchor=None, anchorConfigured=False)
    cells = _opt_t_per_cells(windows, vals[50], deltaText)

    if shouldDisplay:
        assert cells['per1y'] != '—'
        assert cells['per1y_raw'] is not None
        assert cells['per1y_cls'].startswith('opt-t-ivr-')
        assert cells['pera'] == cells['per1y']  # 样本 <252 时 1y 窗与全样本同集合
        assert cells['perc'] == '—'  # 未配锚点
    else:
        for kind in ('per1y', 'perc', 'pera'):
            assert cells[kind] == '—'
            assert cells[f'{kind}_raw'] is None
            assert cells[f'{kind}_cls'] == ''


def test_fmt_opt_daily_qty_compacts_from_one_wan() -> None:
    """≥1 万才缩成一位小数「x.x万」；门槛内整数原样。盘中 delta 走同一函数。"""
    assert _fmt_opt_daily_qty(None) == '—'
    assert _fmt_opt_daily_qty(0) == '0'
    assert _fmt_opt_daily_qty(9999) == '9999'
    assert _fmt_opt_daily_qty(10000) == '1.0万'
    assert _fmt_opt_daily_qty(108377) == '10.8万'
    assert _fmt_opt_daily_qty(996326) == '99.6万'
    assert _fmt_opt_daily_qty(1234.5).startswith('1234')


def testBlack76SpotGammaCallPutEqualAndTwoDecimals() -> None:
    """现货 Γ 与 Δ 同 d1；Call/Put 同值；T 表两位小数。"""
    import math
    import re

    from scipy.stats import norm

    from option_iv import black76_gamma
    from rt_monitor.services.opt_t_board import _opt_t_board_seller_metrics

    s, k, tExp, sig = 3.02, 3.00, 30 / 365.0, 0.20
    gCall = black76_gamma(
        forward=s, strike=k, time_to_expiry=tExp, volatility=sig, spot=s
    )
    gPut = black76_gamma(
        forward=s, strike=k, time_to_expiry=tExp, volatility=sig, spot=s
    )
    assert abs(gCall - gPut) < 1e-15
    d1 = (math.log(s / k) + 0.5 * sig * sig * tExp) / (sig * math.sqrt(tExp))
    expect = float(norm.pdf(d1) / (s * sig * math.sqrt(tExp)))
    assert abs(gCall - expect) < 1e-12

    metrics = _opt_t_board_seller_metrics(
        sellPx=0.12,
        strike=3.0,
        spot=3.02,
        forward=3.02,
        callPut='C',
        daysToExpiry=30,
        rate=0.015,
    )
    assert metrics['gamma'] != '—'
    assert re.fullmatch(r'\d+\.\d{2}', str(metrics['gamma']))
    assert metrics['delta'] != '—'


def testOptTBoardGammaMirrorsDeltaAndSkipsQuoteDelta() -> None:
    """Call：Γ 在 Δ 左；Put 镜像。轻量 quote-delta 不得重算希腊值。"""
    import inspect

    from rt_monitor.services.opt_t_board import queryOptTBoardQuoteDelta

    root = Path(__file__).resolve().parents[1]
    body = (root / 'templates' / 'rt_monitor_opt_t_board_body.html').read_text(
        encoding='utf-8'
    )
    page = (root / 'templates' / 'rt_monitor_opt_t_board.html').read_text(
        encoding='utf-8'
    )
    callGamma = body.index('{{ row.call_gamma }}')
    callDelta = body.index('{{ row.call_delta }}')
    putDelta = body.index('{{ row.put_delta }}')
    putGamma = body.index('{{ row.put_gamma }}')
    assert callGamma < callDelta < putDelta < putGamma
    assert '.opt-t-cg-gamma { width: 2.5%; }' in page
    assert 'flex: 0 1 1560px' in page
    assert 'min-width: 40%;' in page
    src = inspect.getsource(queryOptTBoardQuoteDelta)
    assert 'black76_gamma' not in src
    assert 'implied_volatility' not in src


if __name__ == "__main__":
    unittest.main()

# 代码更新时间：2026-08-25 18:18
# 代码更新时间：2026-08-25 18:27 拒 ETF 占位现货
# 代码更新时间：2026-08-25 18:47 快照拉取 timeout≥1s
# 代码更新时间：2026-08-25 19:34 QMT 链缺口补行单测
# 代码更新时间：2026-08-25 20:41 overlay 按 undl；展示过滤单测
# 代码更新时间：2026-08-26 06:17 摘牌日 SQL >= 单测
# 代码更新时间：2026-08-27 10:51 补行 undl 六位对齐（588000.SH）
# 代码更新时间：2026-08-27 14:57 opt_basic 已有新月不重复补行；QMT 叠同一 ts_code
# 代码更新时间：2026-08-28 06:57 快照 memo 2s 合并 T/IV/预演连打
# 代码更新时间：2026-08-28 07:08 快照 memo 0.5s
# 代码更新时间：2026-08-28 08:04 T型展示全部合约；调整合约分行单测
# 代码更新时间：2026-08-28 09:28 排序先标准后 A、各组行权价升序
# 代码更新时间：2026-08-29 14:58 同月标准/A 量仓与平值分组单测
# 代码更新时间：2026-09-09 16:45 卖价买一/买价卖一、折溢 ask、TDX 盘口源与段头
# 代码更新时间：2026-09-09 17:49 时值&lt;0 全套买方 ask；2.700 认购回归
# 代码更新时间：2026-09-09 18:01 最新列与评估价分离单测
# 代码更新时间：2026-09-09 18:04 夹心区 2.850 用买一单测
# 代码更新时间：2026-09-09 18:13 时值/有效价四位、时年化两位单测
# 代码更新时间：2026-09-15 08:43 T 型段头期限展示回归
# 代码更新时间：2026-09-15 18:25 IVR Delta 展示范围 0.10-0.90 回归
# 代码更新时间：2026-09-15 18:50 本地观测与最后市场事件时钟分离回归
# 代码更新时间：2026-09-20 21:26 IVR 回归改三列分位；星标改浮点比较
# 代码更新时间：2026-09-20 21:51 分位列不标星回归
# 代码更新时间：2026-09-21 06:12 量仓万单位、行权价去星、优选 mid 底回归
# 代码更新时间：2026-09-21 06:22 平值行排除量仓 rank 着色回归
# 代码更新时间：2026-09-21 07:12 T 型 IV≥100 取整、义务仓 ┊ 居中回归
# 代码更新时间：2026-09-21 07:22 桌面左右并排布局回归
# 代码更新时间：2026-09-21 07:34 义务仓单行表头、取消 1760 横滚
# 代码更新时间：2026-09-21 07:52 右栏 1560 可收缩、IV 表 100% 禁横条
# 代码更新时间：2026-09-21 08:07 义务仓 Per 用留白拉开间距
# 代码更新时间：2026-09-21 08:15 Per/☆ 与有效价/┊ 不重叠，档位右留白收
# 代码更新时间：2026-09-23 06:11 chip 现货三位价+涨跌幅；快照说明跟 Per C
# 代码更新时间：2026-09-23 06:46 T 表现货 Γ 两位、Call 在 Δ 左 Put 镜像、不进 quote-delta
# 代码更新时间：2026-09-23 07:09 chip Intra 与主页 Ma/Ema 方向表同式
# 代码更新时间：2026-09-23 09:02 chip Pct/Intra 文案无百分号
# 代码更新时间：2026-09-23 09:31 chip Gap/Per 与计划区 expanding gapP 对齐
# 代码更新时间：2026-09-23 09:53 到期日 DTE=0 仍出 IV/Δ

