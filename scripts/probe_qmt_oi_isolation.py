"""Measure the independent QMT OI POC without changing production quote flow.

Requires the QMT strategy ``QMT_OI_Isolation_POC.py`` to be running and bound
to 127.0.0.1:15575.  The script is read-only: it sends only ping/ticks requests
to the POC and samples the existing 15580 aggregation snapshot.
"""

from __future__ import annotations
# 用途: 一次性诊断
# 保留至: 2026-11-01

import argparse
import json
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import zmq


POC_ADDRESS = "tcp://127.0.0.1:15575"
SNAPSHOT_URL = "http://127.0.0.1:15580/snapshot"
TARGET_UNDERLYINGS = {"510050.SH", "159915.SZ"}


def fetchJson(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=3.0) as response:  # noqa: S310 - loopback fixed by constant/default
        return json.loads(response.read().decode("utf-8"))


def selectTargetCodes(snapshot: dict[str, Any], maximum: int) -> list[str]:
    metaMap = snapshot.get("optMeta") or {}
    sho: list[str] = []
    szo: list[str] = []
    for rawCode, meta in metaMap.items():
        code = str(rawCode).upper()
        undl = str((meta or {}).get("undl") or "").upper()
        if undl not in TARGET_UNDERLYINGS:
            continue
        if code.endswith(".SHO"):
            sho.append(code)
        elif code.endswith(".SZO"):
            szo.append(code)
    sho.sort()
    szo.sort()
    if len(sho) + len(szo) < maximum:
        raise RuntimeError("only %d eligible 510050/159915 codes in snapshot" % (len(sho) + len(szo)))
    # Preserve both exchanges in every batch rather than accidentally testing
    # only the (typically faster) Shanghai contracts.
    shoCount = min(len(sho), (maximum + 1) // 2)
    szoCount = min(len(szo), maximum - shoCount)
    if shoCount + szoCount < maximum:
        shoCount = min(len(sho), maximum - szoCount)
    return sho[:shoCount] + szo[:szoCount]


def parseSnapshotWallAge(snapshot: dict[str, Any], now: float) -> float | None:
    raw = snapshot.get("optWall")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return None
    return max(0.0, now - value)


class SnapshotMonitor:
    def __init__(self, url: str, interval: float = 0.2) -> None:
        self.url = url
        self.interval = interval
        self.samples: list[dict[str, float | None]] = []
        self.errors = 0
        self.stopEvent = threading.Event()
        self.thread = threading.Thread(target=self._run, name="oi-poc-snapshot-monitor", daemon=True)

    def _run(self) -> None:
        while not self.stopEvent.is_set():
            started = time.perf_counter()
            try:
                now = time.time()
                snapshot = fetchJson(self.url)
                self.samples.append({"at": now, "ageSec": parseSnapshotWallAge(snapshot, now)})
            except Exception:
                self.errors += 1
            elapsed = time.perf_counter() - started
            self.stopEvent.wait(max(0.01, self.interval - elapsed))

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopEvent.set()
        self.thread.join(timeout=3.0)

    def summary(self, startAt: float, endAt: float) -> dict[str, Any]:
        ages = [float(item["ageSec"]) for item in self.samples
                if startAt <= float(item["at"] or 0) <= endAt and item["ageSec"] is not None]
        if not ages:
            return {"samples": 0, "errors": self.errors}
        ordered = sorted(ages)
        return {
            "samples": len(ages),
            "errors": self.errors,
            "ageP50Sec": round(statistics.median(ordered), 3),
            "ageP95Sec": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
            "ageMaxSec": round(max(ordered), 3),
        }


def rpcCall(socket: zmq.Socket, payload: dict[str, Any], timeoutSec: float = 20.0) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    socket.send_json(payload)
    if not socket.poll(timeout=int(timeoutSec * 1000)):
        raise TimeoutError("POC RPC timeout after %.1fs" % timeoutSec)
    result = socket.recv_json()
    return result, (time.perf_counter() - started) * 1000.0


def runProbe(rounds: int, baselineSec: float, recoverySec: float) -> dict[str, Any]:
    snapshot = fetchJson(SNAPSHOT_URL)
    codes50 = selectTargetCodes(snapshot, 50)
    monitor = SnapshotMonitor(SNAPSHOT_URL)
    context = zmq.Context.instance()
    socket = context.socket(zmq.REQ)
    socket.linger = 0
    socket.connect(POC_ADDRESS)
    monitor.start()
    try:
        ping, pingMs = rpcCall(socket, {"op": "ping"}, timeoutSec=5.0)
        if not ping.get("ok"):
            raise RuntimeError("POC ping rejected: %s" % ping)
        baselineStart = time.time()
        time.sleep(baselineSec)
        baselineEnd = time.time()
        results: dict[str, list[dict[str, Any]]] = {"6": [], "20": [], "50": []}
        burstStart = time.time()
        for size in (6, 20, 50):
            for index in range(rounds):
                result, wallMs = rpcCall(socket, {"op": "ticks", "codes": codes50[:size]})
                results[str(size)].append({
                    "round": index + 1,
                    "ok": bool(result.get("ok")),
                    "wallMs": round(wallMs, 3),
                    "serviceMs": result.get("elapsedMs"),
                    "requested": result.get("requested"),
                    "returned": result.get("returned"),
                    "oiCovered": result.get("oiCovered"),
                    "error": result.get("error"),
                })
                time.sleep(0.8)
        burstEnd = time.time()
        time.sleep(recoverySec)
        recoveryEnd = time.time()
        return {
            "pocAddress": POC_ADDRESS,
            "pingMs": round(pingMs, 3),
            "selected": {"total": len(codes50), "sho": sum(code.endswith(".SHO") for code in codes50),
                         "szo": sum(code.endswith(".SZO") for code in codes50)},
            "batches": results,
            "mainBridge": {
                "baseline": monitor.summary(baselineStart, baselineEnd),
                "duringBurst": monitor.summary(burstStart, burstEnd),
                "recovery": monitor.summary(burstEnd, recoveryEnd),
            },
        }
    finally:
        monitor.stop()
        socket.close(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--baseline-sec", type=float, default=10.0)
    parser.add_argument("--recovery-sec", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = runProbe(args.rounds, args.baseline_sec, args.recovery_sec)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    failures = [row for rows in report["batches"].values() for row in rows if not row["ok"]]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
