"""海外24代码探针行为契约。"""
from scripts.probe_tdx_overseas_snapshot import TARGETS, classify, exactMatches, progression, isolatedCall, matchesScreenshotIdentity


def test_stem_collision_excluded_from_screenshot_identity():
    assert not matchesScreenshotIdentity("DAX", "DAX.US")
    assert not matchesScreenshotIdentity("HXC", "HXC.US")
    assert matchesScreenshotIdentity("DAX", "DAX.OT")
    assert matchesScreenshotIdentity("HXC", "HXC.OT")


def test_target_count_and_order():
    assert len(TARGETS) == len(set(TARGETS)) == 24
    assert TARGETS[0] == "IXIC" and TARGETS[-1] == "NG00W"


def test_zero_missing_error_and_positive_quote():
    assert classify({"ErrorId": "0", "Now": "0"}) == "no_positive_quote"
    assert classify({"Now": 1}) == "invalid_response"
    assert classify({"ErrorId": "0", "Now": "1.2"}) == "quote_readable"
    assert classify({"ErrorId": "0", "Now": "nan"}) == "no_positive_quote"
    assert classify({"ErrorId": "2"}) == "code_parse_error"
    assert classify({"ErrorId": "7"}) == "no_data"


def test_exact_lookup_not_prefix():
    candidates = [{"Code": "HSI.HI", "Name": "恒生指数"}, {"Code": "HSIL8.OT"}]
    assert exactMatches("HSI", candidates) == [candidates[0]]


def test_missing_or_same_upstream_time_not_live():
    assert progression({"Now": 1}, {"Now": 2})["upstreamTimeAdvanced"] is None
    assert progression({"Time": "12:00", "Now": 1}, {"Time": "12:00", "Now": 2})["upstreamTimeAdvanced"] is False


def test_subprocess_timeout_does_not_raise():
    result = isolatedCall(["python", "-c", "import time; time.sleep(3)"], 0.05)
    assert result["status"] == "subprocess_timeout"
    recovered = isolatedCall(["python", "-c", 'print("{}")'], 3)
    assert recovered["exitCode"] == 0


def test_child_json_evidence_is_retained():
    result = isolatedCall(["python", "-c", 'print("{\\\"Now\\\":1}")'], 3)
    assert result["exitCode"] == 0
    assert result["response"]["Now"] == 1
