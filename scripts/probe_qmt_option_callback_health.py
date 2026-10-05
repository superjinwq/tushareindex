"""核查 QMT 期权 ``subscribe_quote`` 回调健康度。

默认 ``PASSIVE`` 模式只读取外层日志、15580/15581 本地快照及静态桥接配置，
不会探测或连接 15573，也不会创建 QMT RPC 客户端。只有显式 ``--direct-rpc``
才会访问 15573 的三个只读接口；该模式用于短时人工诊断，不宜盘中高频常驻。
"""
from __future__ import annotations
# 用途: 标定
# 保留至: 2026-10-20

import argparse
import json
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, time as dayTime
from pathlib import Path
from typing import Any, Callable


QMT_BRIDGE_SRC = Path(r"D:\cursor\xtquant_big_convert\src")
QMT_CONFIG_PATH = Path(
    r"D:\银河证券QMT实盘 - 交易终端-上海\python\bigqmt_signal_trader_local_config.py"
)
QMT_RPC_SOURCE_PATH = Path(
    r"D:\银河证券QMT实盘 - 交易终端-上海\python\bigqmt_signal_trader\redis_rpc.py"
)
DAQMT_SNAPSHOT_URL = "http://127.0.0.1:15580/snapshot"
TDX_SNAPSHOT_URL = "http://127.0.0.1:15581/snapshot"
QMT_RPC_HOST = "127.0.0.1"
QMT_RPC_PORT = 15573
DAQMT_LOG_PATH = Path(__file__).parents[1] / "大QMT实时行情.log"
READ_ONLY_RPC_METHODS = frozenset(("ping", "get_all_subscription", "get_single_quote_ticks"))


def isReadOnlyRpcMethod(method: str) -> bool:
    """仅允许探针实际调用经过审查的三个只读 RPC。"""
    return str(method or "").strip() in READ_ONLY_RPC_METHODS


def isContinuousAuction(now: datetime | None = None) -> bool:
    current = (now or datetime.now()).time()
    return (
        dayTime(9, 30) <= current < dayTime(11, 30)
        or dayTime(13, 0) <= current < dayTime(14, 57)
    )


def _readText(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _configBool(text: str, key: str) -> bool | None:
    pattern = r"[\"']%s[\"']\s*:\s*(True|False)" % re.escape(key)
    match = re.search(pattern, text)
    if match is None:
        return None
    return match.group(1) == "True"


def auditThreadAffinity(configPath: Path = QMT_CONFIG_PATH, rpcPath: Path = QMT_RPC_SOURCE_PATH) -> dict[str, Any]:
    """静态判定 sync_single_quote 是否可能在后台 ZMQ 线程执行。

    不执行 QMT 文件，仅扫描三个不含凭据的布尔/名单结论；输出也绝不回显配置正文。
    """
    configText = _readText(configPath)
    rpcText = _readText(rpcPath)
    deferredBlock = ""
    match = re.search(r"LISTENER_DEFERRED_METHODS\s*=\s*\{(.*?)\}", rpcText, re.S)
    if match is not None:
        deferredBlock = match.group(1)
    processInListener = _configBool(configText, "rpc_process_in_listener")
    backgroundThreads = _configBool(configText, "rpc_background_threads")
    syncSingleQuoteDeferred = "sync_single_quote" in deferredBlock
    sourceAvailable = bool(configText and rpcText)
    risk = "UNKNOWN"
    if sourceAvailable:
        if processInListener is True and backgroundThreads is True and not syncSingleQuoteDeferred:
            risk = "HIGH"
        elif syncSingleQuoteDeferred:
            risk = "MITIGATED"
        else:
            risk = "LOW_OR_UNCONFIRMED"
    return {
        "sourceAvailable": sourceAvailable,
        "processInListener": processInListener,
        "backgroundThreads": backgroundThreads,
        "syncSingleQuoteDeferred": syncSingleQuoteDeferred,
        "risk": risk,
    }


def _asDict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _countBySuffix(records: dict[str, Any]) -> dict[str, int]:
    counts = {"SHO": 0, "SZO": 0, "other": 0}
    for code in records:
        text = str(code).upper()
        if text.endswith(".SHO"):
            counts["SHO"] += 1
        elif text.endswith(".SZO"):
            counts["SZO"] += 1
        else:
            counts["other"] += 1
    return counts


def _httpJson(url: str, timeoutSec: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeoutSec) as response:
        return _asDict(json.loads(response.read().decode("utf-8", errors="replace")))


def _readLogTail(path: Path = DAQMT_LOG_PATH, maxBytes: int = 131072) -> str:
    """只读末尾日志；文件不存在或轮转时保留可解释的空证据。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - max(1024, int(maxBytes))))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parseLogFields(message: str) -> dict[str, str]:
    return dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", str(message or "")))


def _latestLogMessage(logText: str, prefix: str) -> tuple[str | None, dict[str, str]]:
    for line in reversed(str(logText or "").splitlines()):
        if prefix not in line:
            continue
        message = line.split(" - ", 2)[-1]
        if message.startswith(prefix):
            return line[:23] if len(line) >= 23 else None, _parseLogFields(message)
    return None, {}


def _asInt(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _asFloat(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collectPassiveSample(timeoutSec: float = 5.0, logPath: Path | None = None) -> dict[str, Any]:
    """采样已有进程暴露的证据；此函数绝不调用 QMT RPC。"""
    resolvedLogPath = logPath or DAQMT_LOG_PATH
    errors: list[str] = []
    outerOpt = 0
    tdxOpt = 0
    for label, url in (("outer", DAQMT_SNAPSHOT_URL), ("tdx", TDX_SNAPSHOT_URL)):
        try:
            snapshot = _httpJson(url, timeoutSec)
            count = len(_asDict(snapshot.get("opt")))
            if label == "outer":
                outerOpt = count
            else:
                tdxOpt = count
        except Exception as exc:  # noqa: BLE001 - passive probe retains all other evidence
            errors.append("%s snapshot %s: %s" % (label, type(exc).__name__, exc))
    logText = _readLogTail(resolvedLogPath)
    subAt, sub = _latestLogMessage(logText, "OPT_SUB")
    quoteAt, quote = _latestLogMessage(logText, "OPT_QUOTE_CACHE")
    heartbeatAt, heartbeat = _latestLogMessage(logText, "heartbeat")
    if not logText:
        errors.append("daqmt log unavailable: %s" % resolvedLogPath)
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "source": "PASSIVE_LOG_HTTP",
        "subscriptionObservedAt": subAt,
        "callbackObservedAt": quoteAt,
        "heartbeatObservedAt": heartbeatAt,
        "owned": _asInt(sub.get("owned")),
        "wanted": _asInt(sub.get("wanted")),
        "cached": _asInt(sub.get("cached")),
        "cacheRaw": _asInt(quote.get("raw")),
        "cacheFresh": _asInt(quote.get("sho")),
        "cacheHit": _asInt(quote.get("hit")),
        "cacheStaleEvent": _asInt(quote.get("stale_event")),
        "wholeUnique": _asInt(heartbeat.get("unique")),
        "wholeSilenceSec": _asFloat(heartbeat.get("silence_s")),
        "outerOpt": outerOpt,
        "tdxOpt": tdxOpt,
        "errors": errors,
    }


def isTcpPortOpen(host: str = QMT_RPC_HOST, port: int = QMT_RPC_PORT, timeoutSec: float = 0.5) -> bool:
    """先检查 RPC 端口，避免端口已关闭时每个只读请求都白等一个超时。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.05, float(timeoutSec)))
        return client.connect_ex((host, int(port))) == 0


def _timedRpc(callRpc: Callable[[str, dict[str, Any], float], Any], method: str, params: dict[str, Any], timeoutSec: float) -> tuple[dict[str, Any], float, str | None]:
    if not isReadOnlyRpcMethod(method):
        raise ValueError("probe refused non-read-only RPC: %s" % method)
    started = time.perf_counter()
    try:
        return _asDict(callRpc(method, params, timeoutSec)), (time.perf_counter() - started) * 1000.0, None
    except Exception as exc:  # noqa: BLE001 - live probe must retain the remaining evidence
        return {}, (time.perf_counter() - started) * 1000.0, "%s: %s" % (type(exc).__name__, exc)


def collectSample(callRpc: Callable[[str, dict[str, Any], float], Any], timeoutSec: float = 5.0) -> dict[str, Any]:
    """采样一次，只读 RPC 与两个本地 HTTP 快照相互对照。"""
    ping, pingMs, pingError = _timedRpc(callRpc, "ping", {}, timeoutSec)
    subscriptions, subMs, subError = _timedRpc(
        callRpc, "get_all_subscription", {"period": "tick"}, timeoutSec
    )
    cache, cacheMs, cacheError = _timedRpc(
        callRpc, "get_single_quote_ticks", {"max_age_sec": 30}, timeoutSec
    )
    errors = [message for message in (pingError, subError, cacheError) if message]
    outerOpt = 0
    tdxOpt = 0
    for label, url in (("outer", DAQMT_SNAPSHOT_URL), ("tdx", TDX_SNAPSHOT_URL)):
        try:
            snapshot = _httpJson(url, timeoutSec)
            count = len(_asDict(snapshot.get("opt")))
            if label == "outer":
                outerOpt = count
            else:
                tdxOpt = count
        except Exception as exc:  # noqa: BLE001
            errors.append("%s snapshot %s: %s" % (label, type(exc).__name__, exc))
    owned = subscriptions.get("owned_n", ping.get("owned", 0))
    cached = subscriptions.get("cached_n", ping.get("cached", 0))
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "pingMs": round(pingMs, 1),
        "subscriptionsMs": round(subMs, 1),
        "cacheMs": round(cacheMs, 1),
        "owned": int(owned or 0),
        "cached": int(cached or 0),
        "cacheFresh": len(cache),
        "cacheFreshByMarket": _countBySuffix(cache),
        "cacheUpdates": int(subscriptions.get("cache_updates", ping.get("cache_updates", 0)) or 0),
        "cacheAgeSec": subscriptions.get("cache_age_sec", ping.get("lastAgeSec")),
        "outerOpt": outerOpt,
        "tdxOpt": tdxOpt,
        "errors": errors,
    }


def classifyCallbackHealth(samples: list[dict[str, Any]], threadRisk: str = "UNKNOWN") -> dict[str, str]:
    """仅给出回调状态；机制怀疑与已确认根因严格分离。"""
    comparable = [
        item for item in samples
        if int(item.get("owned") or 0) > 0
        and (int(item.get("outerOpt") or 0) > 0 or int(item.get("tdxOpt") or 0) > 0)
    ]
    suspectedMechanism = "THREAD_AFFINITY" if str(threadRisk) == "HIGH" else "UNCONFIRMED"
    if any(int(item.get("cacheFresh") or 0) > 0 for item in comparable):
        return {
            "status": "CALLBACK_CACHE_ACTIVE",
            "rootCause": "UNCONFIRMED",
            "suspectedMechanism": suspectedMechanism,
        }
    if len(comparable) < 3:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "rootCause": "UNCONFIRMED",
            "suspectedMechanism": suspectedMechanism,
        }
    if all(int(item.get("cacheFresh") or 0) <= 0 for item in comparable):
        return {
            "status": "CALLBACK_CACHE_STALLED",
            "rootCause": "UNCONFIRMED",
            "suspectedMechanism": suspectedMechanism,
        }
    return {
        "status": "INSUFFICIENT_EVIDENCE",
        "rootCause": "UNCONFIRMED",
        "suspectedMechanism": suspectedMechanism,
    }


def _buildRpcCaller() -> Callable[[str, dict[str, Any], float], Any]:
    source = str(QMT_BRIDGE_SRC)
    if source not in sys.path:
        sys.path.insert(0, source)
    from bigqmt_signal_trader.xtquant_compat import configure, xtdata

    try:
        configure(timeout_seconds=5.0)
    except TypeError:
        configure()
    client = getattr(xtdata, "client", None)
    if client is None or not callable(getattr(client, "call", None)):
        raise RuntimeError("QMT RPC client is unavailable")

    def callRpc(method: str, params: dict[str, Any], timeoutSec: float) -> Any:
        if not isReadOnlyRpcMethod(method):
            raise ValueError("probe refused non-read-only RPC: %s" % method)
        return client.call(method, params, timeout_seconds=timeoutSec)

    return callRpc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读核查 QMT 期权 subscribe_quote callback")
    parser.add_argument("--duration", type=float, default=0.0, help="采样时长（秒）；0 表示仅一次")
    parser.add_argument("--interval", type=float, default=5.0, help="采样开始到开始的最小间隔（秒）")
    parser.add_argument("--timeout", type=float, default=5.0, help="单次 HTTP/RPC 超时（秒）")
    parser.add_argument(
        "--direct-rpc",
        action="store_true",
        help="显式访问 15573 的只读 RPC；会与行情进程竞争 ROUTER，不建议盘中常驻",
    )
    args = parser.parse_args(argv)
    threadAudit = auditThreadAffinity()
    report: dict[str, Any] = {
        "mode": "CONTINUOUS_AUCTION" if isContinuousAuction() else "OFF_SESSION_NOT_EVALUATED",
        "threadAffinityAudit": threadAudit,
        "probeMode": "DIRECT_RPC" if args.direct_rpc else "PASSIVE",
        "directRpc": bool(args.direct_rpc),
        "rpcPortChecked": bool(args.direct_rpc),
        "samples": [],
    }
    duration = max(0.0, float(args.duration))
    interval = max(0.5, float(args.interval))
    deadline = time.monotonic() + duration
    callRpc: Callable[[str, dict[str, Any], float], Any] | None = None
    if args.direct_rpc:
        report["rpcPortOpen"] = isTcpPortOpen(timeoutSec=min(0.5, max(0.05, float(args.timeout))))
        if not report["rpcPortOpen"]:
            report["rpcInitError"] = "QMT RPC 127.0.0.1:15573 is not listening"
            report["verdict"] = {"status": "RPC_UNAVAILABLE", "rootCause": "UNCONFIRMED"}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        try:
            callRpc = _buildRpcCaller()
        except Exception as exc:  # noqa: BLE001
            report["rpcInitError"] = "%s: %s" % (type(exc).__name__, exc)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
    while True:
        started = time.monotonic()
        if callRpc is None:
            report["samples"].append(collectPassiveSample(max(0.5, float(args.timeout))))
        else:
            report["samples"].append(collectSample(callRpc, max(0.5, float(args.timeout))))
        if duration <= 0 or time.monotonic() >= deadline:
            break
        waitSec = min(max(0.0, interval - (time.monotonic() - started)), max(0.0, deadline - time.monotonic()))
        if waitSec > 0:
            time.sleep(waitSec)
    if report["mode"] == "CONTINUOUS_AUCTION":
        report["verdict"] = classifyCallbackHealth(report["samples"], threadAudit["risk"])
    else:
        report["verdict"] = {
            "status": "NOT_EVALUATED_OFF_SESSION",
            "rootCause": "UNCONFIRMED",
            "suspectedMechanism": "THREAD_AFFINITY" if threadAudit["risk"] == "HIGH" else "UNCONFIRMED",
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# 代码更新时间：2026-09-17
