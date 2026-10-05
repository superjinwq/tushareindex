# coding: utf-8
"""在独立进程中验证一次 SIGINT；假桥接不会访问真实 QMT 或行情端口。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "大QMT实时行情.py"


def runSandbox(
    mode: str, repeatSignal: bool = False, timeoutSec: float | None = 0.3,
) -> tuple[subprocess.CompletedProcess, float]:
    code = textwrap.dedent(f"""
        import _thread, importlib.util, logging, signal, sys, threading, time, types
        spec = importlib.util.spec_from_file_location('daqmt_shutdown_sandbox', {str(SCRIPT)!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        shutdownTimeout = {timeoutSec!r}
        if shutdownTimeout is not None:
            mod.SHUTDOWN_TIMEOUT_SEC = shutdownTimeout
        mod.TDX_OPT_ENABLE = False
        def forbiddenNetwork(*args, **kwargs):
            raise AssertionError('sandbox must not access production endpoints')
        mod.urllib.request.urlopen = forbiddenNetwork
        mode = {mode!r}
        ready = threading.Event()
        rpcLock = threading.Lock()
        rpcLock.acquire()
        def setup(logger):
            logger.setLevel(logging.INFO)
            logger.addHandler(logging.StreamHandler(sys.stdout))
        mod._setupLogging = setup
        mod.acquireInstanceLockOrSteal = lambda *args: object()
        mod.releaseInstanceLock = lambda *args: print('LOCK_RELEASED', flush=True)
        mod._ensureXtquantSrc = lambda: None
        class Http:
            def shutdown(self):
                print('HTTP_SHUTDOWN', flush=True)
                if mode == 'http_failure':
                    raise RuntimeError('sandbox HTTP shutdown failure')
            def server_close(self):
                print('HTTP_CLOSED', flush=True)
        mod.startQuoteHttp = lambda *args: Http()
        mod.scanQuoteLivePids = lambda: []
        mod.prepareTdxFallbackChain = lambda *args: ['10000001.SHO'] if mode == 'fallback_rpc_wait' else []
        def fallbackLoop(logger, stopEvent):
            print('FALLBACK_STARTED', flush=True)
            ready.set()
            while mod.running and not stopEvent.wait(0.01):
                pass
        mod._runTdxFallbackLoop = fallbackLoop
        def waitRpc(*args):
            if mode != 'fallback_rpc_wait':
                return True
            assert ready.wait(0.5), 'TDX fallback must start before blocking RPC wait'
            print('RPC_WAIT_ENTERED', flush=True)
            while mod.running:
                time.sleep(0.01)
            return False
        mod.waitUntilRpcPing = waitRpc
        mod.subscribeWholeQuoteUntilOk = lambda *args: 123
        mod.resolveEtfOptionCodes = lambda *args, **kwargs: ([], 'sandbox')
        class Session:
            def stop(self):
                print('SESSION_STOPPED', flush=True)
        class XtData:
            _quote_session = Session()
            def unsubscribe_quote(self, seq):
                assert seq == 123
                print('UNSUBSCRIBE_ENTERED', flush=True)
                if mode == 'unsubscribe_block':
                    threading.Event().wait(60)
                if mode == 'unsubscribe_lock':
                    with rpcLock:
                        pass
                print('UNSUBSCRIBE_DONE', flush=True)
        universe = types.ModuleType('bigqmt_signal_trader.quote_universe')
        def buildUniverse(*args):
            if mode == 'startup_block':
                ready.set()
                threading.Event().wait(60)
            return {{'codes': ['510050.SH'], 'counts': {{}}, 'sectors': []}}
        universe.build_quote_universe = buildUniverse
        universe.reject_market_codes = lambda *args: None
        compat = types.ModuleType('bigqmt_signal_trader.xtquant_compat')
        compat.configure = lambda: None
        compat.xt_trader = object()
        compat.xtdata = XtData()
        package = types.ModuleType('bigqmt_signal_trader')
        package.__path__ = []
        sys.modules['bigqmt_signal_trader'] = package
        sys.modules[universe.__name__] = universe
        sys.modules[compat.__name__] = compat
        def watchdog(*args):
            ready.set()
            if mode == 'watchdog_block':
                threading.Event().wait(60)
            while mod.running:
                time.sleep(0.01)
        mod._runWatchdogLoop = watchdog
        def stopOnce():
            assert ready.wait(1.0)
            time.sleep(0.05)
            _thread.interrupt_main(signal.SIGINT)
            if {repeatSignal!r}:
                time.sleep(0.1)
                _thread.interrupt_main(signal.SIGINT)
        if mode == 'service_exception':
            def brokenService(logger):
                raise RuntimeError('sandbox startup error')
            mod._runQuoteService = brokenService
        else:
            threading.Thread(target=stopOnce, daemon=True).start()
        sys.exit(mod.main())
    """)
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8",
            timeout=8.0 if timeoutSec is None else 3.0,
            env=dict(os.environ, PYTHONPATH=str(ROOT)),
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"一次 SIGINT 后进程超过 3 秒仍未退出：{mode}; output={exc.stdout!r}") from exc
    return result, time.monotonic() - started


@pytest.mark.parametrize("mode", ["unsubscribe_block", "unsubscribe_lock", "watchdog_block", "startup_block"])
def testOneSignalExitsEvenWhenBridgeOperationBlocks(mode: str):
    result, elapsed = runSandbox(mode)
    print(json.dumps({"mode": mode, "elapsed_s": round(elapsed, 3), "returncode": result.returncode,
                      "log": result.stdout}, ensure_ascii=False))
    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < 2.0
    assert "SHUTDOWN_TIMEOUT" in result.stdout
    assert result.stdout.count("收到信号 2") == 1
    if mode in ("unsubscribe_block", "unsubscribe_lock"):
        assert "UNSUBSCRIBE_ENTERED" in result.stdout
        assert "UNSUBSCRIBE_DONE" not in result.stdout
        assert "已退订" not in result.stdout


def testHealthyShutdownActuallyReleasesResources():
    result, elapsed = runSandbox("healthy")
    print(json.dumps({"mode": "healthy", "elapsed_s": round(elapsed, 3), "log": result.stdout}, ensure_ascii=False))
    assert result.returncode == 0, result.stdout + result.stderr
    for marker in ("UNSUBSCRIBE_DONE", "HTTP_CLOSED", "SESSION_STOPPED", "LOCK_RELEASED", "SHUTDOWN_COMPLETE"):
        assert marker in result.stdout
    assert "SHUTDOWN_TIMEOUT" not in result.stdout


def testRepeatedSignalDoesNotRestartShutdownDeadline():
    result, elapsed = runSandbox("unsubscribe_block", repeatSignal=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < 2.0
    assert result.stdout.count("收到信号 2") == 1
    assert "SHUTDOWN_TIMEOUT" in result.stdout


def testProductionDefaultShutdownBudgetIsFiveSeconds():
    result, elapsed = runSandbox("unsubscribe_lock", timeoutSec=None)
    print(json.dumps({"mode": "default_budget", "elapsed_s": round(elapsed, 3),
                      "returncode": result.returncode, "log": result.stdout}, ensure_ascii=False))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 4.8 <= elapsed < 7.0
    assert "SHUTDOWN_BEGIN timeout_s=5.0" in result.stdout
    assert "SHUTDOWN_TIMEOUT" in result.stdout


def testFallbackStillStartsBeforeWaitingForQmtRpc():
    result, _ = runSandbox("fallback_rpc_wait")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.index("FALLBACK_STARTED") < result.stdout.index("RPC_WAIT_ENTERED")
    assert "SHUTDOWN_COMPLETE" in result.stdout


def testCleanupFailureIsLoggedAndOtherResourcesStillClose():
    result, _ = runSandbox("http_failure")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sandbox HTTP shutdown failure" in result.stdout
    assert "HTTP_CLOSED" in result.stdout
    assert "SESSION_STOPPED" in result.stdout
    assert "LOCK_RELEASED" in result.stdout
    assert "phase=http state=failed" in result.stdout
    assert "service_exit_code=1" in result.stdout


def testServiceFailureProducesNonzeroExitInsteadOfSilentHang():
    result, _ = runSandbox("service_exception")
    assert result.returncode == 1
    assert "sandbox startup error" in result.stderr + result.stdout


def testHttpShutdownClosesItsListeningPort():
    spec = importlib.util.spec_from_file_location("daqmt_http_shutdown_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mod._stopQuoteHttp(server)
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        server.shutdown()
        server.server_close()


def testWorkerJoinTimeoutDoesNotClaimThreadStopped():
    spec = importlib.util.spec_from_file_location("daqmt_worker_shutdown_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="blocked-test-worker", daemon=True)
    worker.start()
    try:
        with pytest.raises(TimeoutError, match="仍在执行"):
            mod._joinQuoteWorker(worker, 0.01)
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(timeout=1.0)


@pytest.mark.parametrize("rel,old,new,guard", [
    ("大QMT实时行情.py", "serviceDone.wait(timeout=SHUTDOWN_TIMEOUT_SEC)", "serviceDone.wait()", "L082"),
    ("大QMT实时行情.py", 'target=runService, name="daqmt-quote-service", daemon=True',
     'target=runService, name="daqmt-quote-service", daemon=False', "L082"),
    ("tests/test_daqmt_shutdown.py", "mod.urllib.request.urlopen = forbiddenNetwork", "pass", "L083"),
])
def testDeliveryGuardDetectsBrokenShutdownOrNetworkIsolation(monkeypatch, rel, old, new, guard):
    """只在内存中注入缺陷，验证交付守卫确实报 FAIL，不破坏运行脚本。"""
    spec = importlib.util.spec_from_file_location("shutdown_delivery_guard", ROOT / "scripts/selfcheck_delivery.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    readOriginal = check.readText
    assert old in readOriginal(rel)
    def injectedRead(path):
        value = readOriginal(path)
        return value.replace(old, new) if path == rel else value
    monkeypatch.setattr(check, "readText", injectedRead)
    monkeypatch.setattr(check, "getJson", lambda *args, **kwargs: {})
    check.checkLessons()
    assert next(item[2] for item in check.RESULTS if item[1].startswith(guard)) == "FAIL"


# 代码更新时间：2026-10-01 08:04
