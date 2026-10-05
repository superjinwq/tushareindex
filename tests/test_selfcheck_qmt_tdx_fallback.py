# coding: utf-8
"""运行态自检对 QMT 主桥 / TDX-only 降级的判据回归。"""
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


def makeFallbackSnapshot(missingChain: str = "", noTdxChain: str = "") -> dict:
    """构造五链均有一个 TDX 可观测字段的最小有效快照。"""
    meta = {}
    opt = {}
    for i, undl in enumerate(("510050", "510300", "510500", "588000", "159915")):
        if undl == missingChain:
            continue
        code = f"TEST{i:02d}.SHO"
        meta[code] = {"undl": f"{undl}.SH"}
        opt[code] = ({"lastSource": "qmt"} if undl == noTdxChain else
                     {"lastSource": "tdx", "bookSource": "tdx", "qtySource": "tdx"})
    return {"optMeta": meta, "opt": opt}


class QmtTdxFallbackSelfcheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import selfcheck_delivery as sc

        cls.sc = sc
        cls.ports = {5000: True, 15580: True, 15581: True,
                     15573: False, 15574: False}

    def testQmtOfflineWithFiveChainTdxSnapshotIsDegradedButAvailable(self):
        ok, evidence = self.sc.tdxFallbackRuntimeHealth(
            self.ports, makeFallbackSnapshot()
        )
        self.assertTrue(ok)
        self.assertIn("510050:1/1", evidence)
        self.assertIn("159915:1/1", evidence)

    def testMissingChainCannotBeHiddenAsHealthyFallback(self):
        ok, evidence = self.sc.tdxFallbackRuntimeHealth(
            self.ports, makeFallbackSnapshot(missingChain="159915")
        )
        self.assertFalse(ok)
        self.assertIn("159915", evidence)

    def testQmtLabeledFieldIsNotProofOfTdxFallback(self):
        ok, evidence = self.sc.tdxFallbackRuntimeHealth(
            self.ports, makeFallbackSnapshot(noTdxChain="510500")
        )
        self.assertFalse(ok)
        self.assertIn("510500", evidence)

    def testTdxClientPortIsMandatoryForFallback(self):
        ports = dict(self.ports)
        ports[15581] = False
        ok, evidence = self.sc.tdxFallbackRuntimeHealth(ports, makeFallbackSnapshot())
        self.assertFalse(ok)
        self.assertIn("15581", evidence)

    def testOffSessionCrossDayEmptySnapshotIsSkipNotFalseHealthy(self):
        verdict, mode, evidence = self.sc.coreQuoteRuntimeHealth(
            self.ports, {"optMeta": makeFallbackSnapshot()["optMeta"], "opt": {}}, False
        )
        self.assertIsNone(verdict)
        self.assertEqual(mode, "盘外待市")
        self.assertIn("安全过滤", evidence)

    def testSameEmptySnapshotMustFailDuringTradingSession(self):
        verdict, mode, evidence = self.sc.coreQuoteRuntimeHealth(
            self.ports, {"optMeta": makeFallbackSnapshot()["optMeta"], "opt": {}}, True
        )
        self.assertFalse(verdict)
        self.assertEqual(mode, "不可用")
        self.assertIn("缺降级覆盖", evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
