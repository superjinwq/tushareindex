"""独立只读海外证券双入口取证，不刷新缓存、不写库。"""
from __future__ import annotations
# 用途: 一次性诊断
# 保留至: 2026-11-01
import argparse
import contextlib
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

TARGETS = "IXIC DIA SPY NDX DAX OMXS30 HSI HSCEI HSTECH HSIL8 IWM HXC NBI SOX MCAL8 CNY0 NK0Y YM00W USDCNH GC00W SI00W CL00W HG00W NG00W".split()
CONTROLS = ["932000.CSI", "600519.SH"]


def matchesScreenshotIdentity(stem: str, code: str) -> bool:
    """本次type7检索及截图名称核验所得映射，不作通用市场推断。"""
    verified = {symbol: f"{symbol}.OT" for symbol in TARGETS}
    verified.update({symbol: f"{symbol}.US" for symbol in ("DIA", "SPY", "IWM")})
    verified.update({symbol: f"{symbol}.HI" for symbol in ("HSI", "HSCEI", "HSTECH")})
    verified.update({symbol: symbol for symbol in CONTROLS})
    return verified.get(stem) == code


def classify(response: Any) -> str:
    if not isinstance(response, dict) or "ErrorId" not in response:
        return "invalid_response"
    error = str(response["ErrorId"])
    if error != "0":
        return {"2": "code_parse_error", "6": "server_timeout", "7": "no_data"}.get(error, "api_error")
    try:
        now = response.get("Now")
        valid = not isinstance(now, bool) and math.isfinite(float(now)) and float(now) > 0
    except (TypeError, ValueError):
        valid = False
    return "quote_readable" if valid else "no_positive_quote"


def candidateCode(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("Code", "code", "stock_code", "StockCode", "code_str", "CodeStr"):
            if isinstance(item.get(key), str):
                return item[key]
    return ""


def exactMatches(stem: str, candidates: Any) -> list[Any]:
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if candidateCode(item).split(".")[0].upper() == stem.split(".")[0].upper()]


def progression(first: dict, second: dict) -> dict:
    timeKeys = [key for key in first if any(word in key.lower() for word in ("time", "date")) and key in second]
    return {"upstreamTimeFields": timeKeys, "upstreamTimeAdvanced": any(first[k] != second[k] for k in timeKeys) if timeKeys else None, "changedFields": [k for k in first if k in second and first[k] != second[k]], "continuousRealtimeVerified": False}


def utcNow() -> str:
    return datetime.now(timezone.utc).isoformat()


def isolatedCall(command: list[str], timeout: float) -> dict:
    started = time.monotonic()
    record = {"startedAtUtc": utcNow()}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf8", timeout=timeout, check=False)
        record.update(exitCode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr, status="completed")
        try:
            record["response"] = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            record["parseError"] = str(exc)
    except subprocess.TimeoutExpired as exc:
        record.update(status="subprocess_timeout", exitCode=None, stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""))
    record.update(endedAtUtc=utcNow(), elapsedSec=round(time.monotonic()-started, 4))
    return record


def worker(args: argparse.Namespace) -> dict:
    handles = [os.add_dll_directory(str(args.tdx_root)), os.add_dll_directory(str(args.tdx_root / "PYPlugins"))]
    sys.path.insert(0, str(args.tdx_root / "PYPlugins/user"))
    log = io.StringIO()
    result: dict[str, Any] = {"requestedSymbol": args.symbol, "snapshots": [], "wrapperTimeoutClampedMs": args.call_timeout_ms}
    with contextlib.redirect_stdout(log):
        from tqcenter import dll, tq
        native = dll.GetTdxDataStr
        ownPath = str(Path(__file__).resolve()) + f".pid{os.getpid()}"
        tq.initialize(ownPath)
        def rawCall(request: dict) -> dict:
            request["id"] = tq._get_run_id()
            row = {"request": request.copy(), "startedAtUtc": utcNow()}
            started = time.monotonic()
            raw = native(tq._get_run_id(), json.dumps(request).encode("utf8"), args.call_timeout_ms)
            row["rawText"] = raw.decode("utf8", errors="replace") if raw else ""
            try:
                row["response"] = json.loads(row["rawText"])
            except json.JSONDecodeError as exc:
                row["parseError"] = str(exc)
            row.update(endedAtUtc=utcNow(), elapsedSec=round(time.monotonic()-started, 4))
            return row
        captured: list[dict] = []
        def clampedCall(runId: int, payload: bytes, timeoutMs: int) -> bytes:
            started = time.monotonic()
            raw = native(runId, payload, min(timeoutMs, args.call_timeout_ms))
            captured.append({"request": json.loads(payload), "rawText": raw.decode("utf8", errors="replace") if raw else "", "elapsedSec": round(time.monotonic()-started, 4), "requestedTimeoutMs": timeoutMs, "effectiveTimeoutMs": min(timeoutMs, args.call_timeout_ms)})
            return raw
        try:
            if args.sample_code:
                codes = [args.sample_code]
            elif args.symbol in CONTROLS:
                codes = [args.symbol]
            else:
                lookup = rawCall({"type": 7, "key_word": args.symbol})
                result["lookup"] = lookup
                matches = exactMatches(args.symbol, lookup.get("response", {}).get("Value"))
                result["exactMatches"] = matches
                codes = list(dict.fromkeys(candidateCode(x) for x in matches))[:2]
            result["canonicalCodes"] = codes
            for code in codes:
                row = {"code": code, "matchesScreenshotIdentity": matchesScreenshotIdentity(args.symbol, code), "native": rawCall({"type": 3, "stock_code": code})}
                row["native"]["classification"] = classify(row["native"].get("response"))
                captured.clear()
                dll.GetTdxDataStr = clampedCall
                started = time.monotonic()
                public = {"startedAtUtc": utcNow()}
                try:
                    public["response"] = tq.get_market_snapshot(code)
                    public["classification"] = classify(public["response"])
                except (ValueError, RuntimeError, OSError) as exc:
                    public.update(exception={"type": type(exc).__name__, "message": str(exc)}, classification="wrapper_exception")
                finally:
                    dll.GetTdxDataStr = native
                public.update(endedAtUtc=utcNow(), elapsedSec=round(time.monotonic()-started, 4), nativeCalls=captured.copy())
                row["publicWrapper"] = public
                result["snapshots"].append(row)
                if not getattr(tq, "_initialized", False):
                    tq.initialize(ownPath)
        finally:
            dll.GetTdxDataStr = native
            tq.close()
            result["ownConnectionClosed"] = True
            for handle in handles:
                handle.close()
    result["sdkLog"] = log.getvalue()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tdx-root", type=Path, default=Path("D:/new_tdx"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--call-timeout-ms", type=int, default=3000, choices=range(100, 3001), metavar="100..3000")
    parser.add_argument("--samples", type=int, default=2, choices=(1, 2, 3))
    parser.add_argument("--sample-interval-sec", type=float, default=30)
    parser.add_argument("--budget-sec", type=float, default=360)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--symbol", help=argparse.SUPPRESS)
    parser.add_argument("--sample-code", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args), ensure_ascii=False))
        return 0
    if args.output_dir is None:
        parser.error("--output-dir required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    dllPath = args.tdx_root / "PYPlugins/TPythClient.dll"
    report = {"startedAtUtc": utcNow(), "dllPath": str(dllPath), "dllSha256": hashlib.sha256(dllPath.read_bytes()).hexdigest(), "refreshCalled": False, "symbols": [], "repeatSamples": []}
    output = args.output_dir / "tdx-overseas-results.json"
    started = time.monotonic()
    def save() -> None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    def call(symbol: str, code: str = "") -> dict:
        remaining = args.budget_sec - (time.monotonic()-started)
        if remaining <= 0:
            return {"status": "budget_exhausted", "exitCode": None, "elapsedSec": 0}
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker", "--symbol", symbol, "--tdx-root", str(args.tdx_root), "--call-timeout-ms", str(args.call_timeout_ms)]
        if code:
            command += ["--sample-code", code]
        return isolatedCall(command, min(20, remaining))
    for symbol in CONTROLS + TARGETS:
        record = {"symbol": symbol, "control": symbol in CONTROLS, "call": call(symbol)}
        report["symbols"].append(record)
        save()
        response = record["call"].get("response", {})
        rows = response.get("snapshots", [])
        print(symbol, ", ".join(f"{r['code']} native={r['native']['classification']} wrapper={r['publicWrapper']['classification']}" for r in rows) or record["call"]["status"]+" no exact canonical hit", flush=True)
    for record in report["symbols"]:
        for snap in record["call"].get("response", {}).get("snapshots", []):
            if not matchesScreenshotIdentity(record["symbol"], snap["code"]):
                continue
            if snap["native"]["classification"] != "quote_readable":
                continue
            previousStart = time.monotonic()
            for sample in range(1, args.samples):
                delay = args.sample_interval_sec - (time.monotonic()-previousStart)
                if time.monotonic()-started + max(0, delay) >= args.budget_sec:
                    report["repeatBudgetExhausted"] = True
                    break
                if delay > 0:
                    time.sleep(delay)
                previousStart = time.monotonic()
                repeat = {"symbol": record["symbol"], "code": snap["code"], "sample": sample+1, "call": call(record["symbol"], snap["code"])}
                repeats = repeat["call"].get("response", {}).get("snapshots", [])
                if repeats:
                    repeat["progression"] = progression(snap["native"].get("response", {}), repeats[0]["native"].get("response", {}))
                report["repeatSamples"].append(repeat)
                save()
    report["endedAtUtc"] = utcNow()
    save()
    with (args.output_dir / "tdx-overseas-results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["symbol", "canonicalCode", "matchesScreenshotIdentity", "native", "publicWrapper", "Now", "elapsedSec"])
        writer.writeheader()
        for record in report["symbols"]:
            rows = record["call"].get("response", {}).get("snapshots", [])
            for snap in rows or [{}]:
                writer.writerow({"symbol": record["symbol"], "canonicalCode": snap.get("code", ""), "matchesScreenshotIdentity": matchesScreenshotIdentity(record["symbol"], snap.get("code", "")), "native": snap.get("native", {}).get("classification", record["call"]["status"]), "publicWrapper": snap.get("publicWrapper", {}).get("classification", "not_called_no_exact_match"), "Now": snap.get("native", {}).get("response", {}).get("Now"), "elapsedSec": record["call"]["elapsedSec"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# 代码更新时间：2026-10-02
