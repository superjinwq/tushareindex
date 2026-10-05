"""只读 QMT 期权订阅回调探针的行为回归。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def probeModule():
    path = Path(__file__).parents[1] / "scripts" / "probe_qmt_option_callback_health.py"
    spec = importlib.util.spec_from_file_location("qmtOptionCallbackProbe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_only_allowlist_rejects_subscription_mutation(probeModule):
    assert probeModule.isReadOnlyRpcMethod("ping") is True
    assert probeModule.isReadOnlyRpcMethod("get_all_subscription") is True
    assert probeModule.isReadOnlyRpcMethod("get_single_quote_ticks") is True
    assert probeModule.isReadOnlyRpcMethod("sync_single_quote") is False
    assert probeModule.isReadOnlyRpcMethod("unsubscribe_quote") is False


def test_static_audit_identifies_background_thread_affinity_risk(probeModule, tmp_path):
    configPath = tmp_path / "config.py"
    rpcPath = tmp_path / "redis_rpc.py"
    configPath.write_text(
        '"rpc_process_in_listener": True,\n"rpc_background_threads": True,\n',
        encoding="utf-8",
    )
    rpcPath.write_text(
        "LISTENER_DEFERRED_METHODS = {\n    'sync_positions',\n}\n"
        "def _handle_sync_single_quote(self):\n    pass\n",
        encoding="utf-8",
    )

    audit = probeModule.auditThreadAffinity(configPath, rpcPath)

    assert audit["processInListener"] is True
    assert audit["backgroundThreads"] is True
    assert audit["syncSingleQuoteDeferred"] is False
    assert audit["risk"] == "HIGH"


def test_callback_health_marks_empty_cache_as_stalled_not_proven_root_cause(probeModule):
    samples = [
        {"owned": 396, "cacheFresh": 0, "outerOpt": 664, "tdxOpt": 654},
        {"owned": 396, "cacheFresh": 0, "outerOpt": 664, "tdxOpt": 654},
        {"owned": 396, "cacheFresh": 0, "outerOpt": 664, "tdxOpt": 654},
    ]

    verdict = probeModule.classifyCallbackHealth(samples, threadRisk="HIGH")

    assert verdict["status"] == "CALLBACK_CACHE_STALLED"
    assert verdict["rootCause"] == "UNCONFIRMED"
    assert verdict["suspectedMechanism"] == "THREAD_AFFINITY"


def test_callback_health_does_not_claim_failure_when_cache_moves(probeModule):
    samples = [
        {"owned": 396, "cacheFresh": 200, "outerOpt": 664, "tdxOpt": 654},
        {"owned": 396, "cacheFresh": 205, "outerOpt": 664, "tdxOpt": 654},
    ]

    verdict = probeModule.classifyCallbackHealth(samples)

    assert verdict["status"] == "CALLBACK_CACHE_ACTIVE"
    assert verdict["rootCause"] == "UNCONFIRMED"


def test_passive_default_never_opens_qmt_rpc_or_builds_client(probeModule, monkeypatch, capsys, tmp_path):
    logPath = tmp_path / "大QMT实时行情.log"
    logPath.write_text(
        "2026-09-17 13:08:25,332 - INFO - OPT_QUOTE_CACHE raw=433 stale_event=0 hit=411 szo=0 sho=433\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(probeModule, "DAQMT_LOG_PATH", logPath)
    monkeypatch.setattr(probeModule, "isTcpPortOpen", lambda *args, **kwargs: pytest.fail("passive opened 15573"))
    monkeypatch.setattr(probeModule, "_buildRpcCaller", lambda: pytest.fail("passive built RPC client"))
    monkeypatch.setattr(probeModule, "_httpJson", lambda *args, **kwargs: {"opt": {"A.SHO": {}}})

    assert probeModule.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["probeMode"] == "PASSIVE"
    assert report["directRpc"] is False
    assert report["rpcPortChecked"] is False
    assert report["samples"][0]["cacheRaw"] == 433


def test_direct_rpc_requires_explicit_flag(probeModule, monkeypatch, capsys):
    calls = []

    monkeypatch.setattr(probeModule, "isTcpPortOpen", lambda *args, **kwargs: True)
    monkeypatch.setattr(probeModule, "_buildRpcCaller", lambda: (lambda method, params, timeout: calls.append(method) or {}))
    monkeypatch.setattr(probeModule, "_httpJson", lambda *args, **kwargs: {"opt": {"A.SHO": {}}})

    assert probeModule.main(["--direct-rpc"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["probeMode"] == "DIRECT_RPC"
    assert report["rpcPortChecked"] is True
    assert calls == ["ping", "get_all_subscription", "get_single_quote_ticks"]
