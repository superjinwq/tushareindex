"""`audit_opt_freshness --slo` 的纯逻辑回归。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from scripts import audit_opt_freshness as audit


def _field(observedMs: float, value: float = 1.0) -> dict:
    return {"replace": True, "observed_ms": observedMs, "value": value}


def _payload(now: datetime) -> dict:
    observedMs = now.timestamp() * 1000.0 - 2_000.0
    return {
        "ok": True,
        "quotes": {
            "10000001.SH": {"last": _field(observedMs), "vol": _field(observedMs), "oi": _field(observedMs), "book": _field(observedMs)},
            "10000002.SH": {"last": _field(observedMs), "vol": _field(observedMs), "oi": _field(observedMs), "book": _field(observedMs)},
            "10000003.SH": {"last": _field(observedMs), "vol": _field(observedMs), "oi": _field(observedMs), "book": _field(observedMs)},
            "10000004.SH": {"last": _field(observedMs), "vol": _field(observedMs), "oi": _field(observedMs), "book": _field(observedMs)},
        },
    }


def _snapshot() -> dict:
    return {
        "etf": {"510050.SH": {"last": 2.5}},
        "optMeta": {
            "10000001.SHO": {"undl": "510050.SH", "k": 2.4, "cp": "C"},
            "10000002.SHO": {"undl": "510050.SH", "k": 2.4, "cp": "P"},
            "10000003.SHO": {"undl": "510050.SH", "k": 2.5, "cp": "C"},
            "10000004.SHO": {"undl": "510050.SH", "k": 2.5, "cp": "P"},
        },
    }


def testContinuousAuctionBoundaries() -> None:
    assert not audit.inContinuousAuction(datetime(2026, 9, 14, 9, 29, 59))
    assert audit.inContinuousAuction(datetime(2026, 9, 14, 9, 30))
    assert audit.inContinuousAuction(datetime(2026, 9, 14, 11, 30))
    assert not audit.inContinuousAuction(datetime(2026, 9, 14, 11, 30, 1))
    assert audit.inContinuousAuction(datetime(2026, 9, 14, 13, 0))
    assert not audit.inContinuousAuction(datetime(2026, 9, 14, 15, 0, 1))


def testDeltaSloPassesOnlyWithFreshObservableOi() -> None:
    now = datetime(2026, 9, 14, 10, 0)
    with patch.object(audit, "getJson", return_value=_payload(now)):
        passed, label = audit.auditDeltaSlo("510050", _snapshot(), now)

    assert passed is True
    assert label == "PASS"


def testDeltaSloDoesNotRateOffSession() -> None:
    now = datetime(2026, 9, 14, 20, 0)
    with patch.object(audit, "getJson", return_value=_payload(now)):
        passed, label = audit.auditDeltaSlo("510050", _snapshot(), now)

    assert passed is None
    assert label == "NOT_EVALUATED_OFF_SESSION"
