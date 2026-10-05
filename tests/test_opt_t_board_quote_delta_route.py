"""轻量报价 delta 路由的 HTTP 契约回归。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from flask import Flask

from rt_monitor.blueprints import opt_t_board_bp as boardBp


class OptTBoardQuoteDeltaRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(boardBp.optTBoardBp)
        self.client = app.test_client()

    def testDeltaRouteDoesNotInvokeHeavyBoardPayload(self) -> None:
        delta = {
            "ok": True,
            "u": "510050",
            "poll_ms": 1000,
            "quotes": {
                "10010971.SH": {
                    "last": {"text": "0.1234", "replace": True},
                    "vol": {"text": "123", "replace": True},
                    "oi": {"text": "456", "replace": True},
                    "book": {"bid1": "0.1230", "ask1": "0.1238", "replace": True},
                }
            },
        }
        with (
            patch.object(boardBp, "queryOptTBoardQuoteDelta", return_value=delta) as queryDelta,
            patch.object(boardBp, "queryOptTBoardPayload", side_effect=AssertionError("heavy payload forbidden")),
        ):
            resp = self.client.get("/api/opt-t-board/quote-delta?u=510050")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), delta)
        self.assertEqual(resp.headers.get("X-Opt-Delta-Poll-Ms"), "1000")
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))
        queryDelta.assert_called_once_with("510050")

    def testDeltaRouteRejectsInvalidUnderlyingBeforeSnapshotRead(self) -> None:
        with patch.object(boardBp, "queryOptTBoardQuoteDelta") as queryDelta:
            resp = self.client.get("/api/opt-t-board/quote-delta?u=not-a-chain")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])
        queryDelta.assert_not_called()


if __name__ == "__main__":
    unittest.main()
