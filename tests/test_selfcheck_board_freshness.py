# coding: utf-8
"""T 表自检必须按 quote-delta 的字段时钟统计，不能猜 HTML 单元格数量。"""
from __future__ import annotations

import os
import sys
import unittest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO, "scripts")
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def quote(ageClass: str = "") -> dict:
    return {"age_class": ageClass}


class SelfcheckBoardFreshnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import selfcheck_delivery as sc

        cls.sc = sc

    def testStructuredAgeSummarySeparatesFieldCellsFromFullyStaleRows(self):
        payload = {
            "quotes": {
                "A": {field: quote() for field in ("last", "vol", "oi", "book")},
                "B": {"last": quote(), "vol": quote(), "oi": quote("opt-t-age-stale"), "book": quote()},
                "C": {"last": quote("opt-t-age-stale"), "vol": quote("opt-t-age-stale"), "oi": quote("opt-t-age-stale"), "book": quote()},
                "D": {field: quote("opt-t-age-ancient") for field in ("last", "vol", "oi", "book")},
            }
        }

        summary = self.sc.summarizeBoardQuoteDeltaAges(payload)

        self.assertEqual(summary["quoteN"], 4)
        self.assertEqual(summary["fullStaleRowN"], 1)
        self.assertEqual(summary["fieldStaleN"], {
            "last": 2, "vol": 2, "oi": 3, "book": 1,
        })
        self.assertEqual(summary["fieldAncientN"], {
            "last": 1, "vol": 1, "oi": 1, "book": 1,
        })

    def testMissingFieldFailsClosedForTheRowAndThatField(self):
        summary = self.sc.summarizeBoardQuoteDeltaAges({"quotes": {"A": {}}})

        self.assertEqual(summary["quoteN"], 1)
        self.assertEqual(summary["fullStaleRowN"], 1)
        self.assertEqual(summary["fieldStaleN"], {
            "last": 1, "vol": 1, "oi": 1, "book": 1,
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
