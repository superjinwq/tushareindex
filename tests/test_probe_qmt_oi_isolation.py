from scripts.probe_qmt_oi_isolation import parseSnapshotWallAge, selectTargetCodes


def testSelectTargetCodesBalancesShanghaiAndShenzhen() -> None:
    snapshot = {
        "optMeta": {
            **{"1001%04d.SHO" % index: {"undl": "510050.SH"} for index in range(30)},
            **{"9000%04d.SZO" % index: {"undl": "159915.SZ"} for index in range(30)},
            "10019999.SHO": {"undl": "510300.SH"},
        }
    }
    selected = selectTargetCodes(snapshot, 20)
    assert len(selected) == 20
    assert sum(code.endswith(".SHO") for code in selected) == 10
    assert sum(code.endswith(".SZO") for code in selected) == 10


def testParseSnapshotWallAgeRejectsMalformedValue() -> None:
    assert parseSnapshotWallAge({"optWall": "not-a-date"}, 1_789_449_000.0) is None


def testParseSnapshotWallAgeUsesLocalIsoWallClock() -> None:
    age = parseSnapshotWallAge({"optWall": "2026-09-15T13:30:00"}, 1_789_455_000.0)
    assert age is not None
    assert age >= 0
