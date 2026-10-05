"""原油探针的错误与行情判定边界。"""
import pytest
from scripts.probe_tdx_crude_connectivity import classifySnapshot, decodeResponse, extractSuffixes


def test_delivery_guard_detects_suffix_period_mutation(monkeypatch):
    from scripts import probe_tdx_crude_connectivity as probe
    from scripts import selfcheck_delivery as delivery

    monkeypatch.setattr(probe, "extractSuffixes", lambda blob: ["SZ", "SH", "BJ", "NYM", "5m"])
    monkeypatch.setattr(delivery, "RESULTS", [])
    delivery.checkLessons()
    results = [row for row in delivery.RESULTS if "L084" in row[1]]
    assert len(results) == 1
    assert results[0][2] == "FAIL"


def test_suffix_table_stops_before_period():
    table = b"SZ\x00\x00SH\x00\x00BJ\x00\x00NQ\x00\x00SZO\x00SHO\x00HK\x00\x00US\x00\x00CSI\x00CNI\x00HG\x00\x00CFF\x00SHF\x00DCE\x00CZC\x00INE\x00GFE\x00HI\x00\x005m"
    suffixes = extractSuffixes(table)
    assert len(suffixes) == 18
    assert suffixes[-1] == "HI"
    assert "5m" not in suffixes


@pytest.mark.parametrize("response, expected", [
    ({"ErrorId": "0", "Now": 88.84}, "quote_readable"),
    ({"ErrorId": 0, "Now": "88.84"}, "quote_readable"),
    ({"Now": 88.84}, "invalid_response"),
    ({"ErrorId": "2", "Now": 88.84}, "code_parse_error"),
    ({"ErrorId": "7"}, "no_data"),
    ({"ErrorId": "5"}, "api_error"),
    ({"ErrorId": "0", "Now": True}, "success_without_positive_quote"),
    ({"ErrorId": "0", "Now": float("nan")}, "success_without_positive_quote"),
    ({"ErrorId": "0", "Now": 0}, "success_without_positive_quote"),
    ({"ErrorId": "0"}, "success_without_positive_quote"),
])
def test_classification(response, expected):
    assert classifySnapshot(response) == expected


def test_raw_evidence_retains_unknown_time_field():
    raw = b'{"ErrorId":"0","Now":88.84,"QuoteTime":"original"}'
    decoded = decodeResponse(raw)
    assert decoded["rawText"] == raw.decode()
    assert decoded["response"]["QuoteTime"] == "original"
    assert "quoteTimestamp" not in decoded


def test_malformed_response():
    assert decodeResponse(None)["decodeError"] == "empty_pointer"
    assert "decodeError" in decodeResponse(b"bad json")
