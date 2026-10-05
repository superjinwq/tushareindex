"""有界只读 TQ 原油代码取证；不刷新缓存、不写数据库。

用途: 一次性诊断
保留至: 2026-10-31
"""
from __future__ import annotations
# 用途: 留档
# 保留至: 2026-10-31

import argparse
import ast
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any


def extractSuffixes(tableBytes: bytes) -> list[str]:
    """连续后缀表在周期字符串前终止。"""
    suffixes = []
    for part in tableBytes.split(b"\x00"):
        if not part:
            continue
        token = part.decode("ascii", errors="replace")
        if not re.fullmatch(r"[A-Z]{2,3}", token):
            break
        suffixes.append(token)
    return suffixes


def classifySnapshot(response: Any) -> str:
    """仅 ErrorId=0 且有限正数 Now 才确认报价可读。"""
    if not isinstance(response, dict) or "ErrorId" not in response:
        return "invalid_response"
    errorId = str(response["ErrorId"])
    if errorId == "2":
        return "code_parse_error"
    if errorId == "7":
        return "no_data"
    if errorId != "0":
        return "api_error"
    now = response.get("Now")
    try:
        valid = not isinstance(now, bool) and math.isfinite(float(now)) and float(now) > 0
    except (TypeError, ValueError, OverflowError):
        valid = False
    return "quote_readable" if valid else "success_without_positive_quote"


def decodeResponse(raw: bytes | None) -> dict[str, Any]:
    if not raw:
        return {"decodeError": "empty_pointer"}
    rawText = raw.decode("utf-8", errors="replace")
    try:
        return {"rawText": rawText, "response": json.loads(rawText)}
    except json.JSONDecodeError as exc:
        return {"rawText": rawText, "decodeError": str(exc)}


def utcNow() -> str:
    return datetime.now(timezone.utc).isoformat()


def collectEvidence(tdxRoot: Path, evidencePath: Path) -> int:
    userDir = tdxRoot / "PYPlugins/user"
    sourcePath = userDir / "tqcenter.py"
    dllPath = tdxRoot / "PYPlugins/TPythClient.dll"
    source = sourcePath.read_text(encoding="utf-8-sig")
    syntax = ast.parse(source)
    suffixMap = {}
    for node in ast.walk(syntax):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "suffix_map" for t in node.targets):
            suffixMap = ast.literal_eval(node.value)
            break
    dllBytes = dllPath.read_bytes()
    offset = dllBytes.find(b"SZ\x00\x00SH\x00\x00BJ\x00")
    tableBytes = dllBytes[offset:offset + 74] if offset >= 0 else b""
    dllSuffixes = extractSuffixes(tableBytes)
    dayPath = tdxRoot / "vipdoc/ds/lday/17#CL00W.day"
    marketPath = tdxRoot / "dsmarket.dat"
    marketBytes = marketPath.read_bytes()
    marketLines = marketBytes.decode("gbk", errors="replace").splitlines()
    marketEvidence = [line for line in marketLines if line.startswith(("GUIMarketSet02=", "GUIMarketName02="))]
    evidence: dict[str, Any] = {
        "startedAtUtc": utcNow(), "python": sys.version,
        "source": {"path": str(sourcePath), "sha256": hashlib.sha256(source.encode()).hexdigest(), "suffixMap": suffixMap},
        "dll": {"path": str(dllPath), "sha256": hashlib.sha256(dllBytes).hexdigest(), "suffixTableOffset": offset, "suffixTableHex": tableBytes.hex(), "suffixes": dllSuffixes},
        "localIdentity": {"dayFile": str(dayPath), "exists": dayPath.exists(), "mtimeUtc": datetime.fromtimestamp(dayPath.stat().st_mtime, timezone.utc).isoformat() if dayPath.exists() else None, "size": dayPath.stat().st_size if dayPath.exists() else None, "marketNumber": 17, "displayCode": "CL00W"},
        "marketConfig": {"path": str(marketPath), "sha256": hashlib.sha256(marketBytes).hexdigest(), "pairedCategoryLines": marketEvidence},
        "enumerationSemantics": "market is category selector: 101 domestic futures, 102 HK, 103 US; 17 is research industry level 2, not NYMEX exchange",
        "enumerations": [], "snapshots": [], "refreshCalled": False,
    }
    def save() -> None:
        evidencePath.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    sys.path.insert(0, str(userDir))
    from tqcenter import dll, tq
    initialized = False
    try:
        tq.initialize(str(Path(__file__).resolve()) + f".pid{os.getpid()}")
        initialized = True
        evidence["connection"] = {"initialized": True, "runId": tq._get_run_id()}
        def snapshot(code: str, origin: str) -> None:
            row: dict[str, Any] = {"code": code, "origin": origin, "startedAtUtc": utcNow()}
            started = time.monotonic()
            row.update(decodeResponse(dll.GetREPORTInStr(tq._get_run_id(), code.encode(), 3000)))
            row.update(endedAtUtc=utcNow(), elapsedSec=round(time.monotonic()-started, 4))
            row["classification"] = classifySnapshot(row.get("response"))
            evidence["snapshots"].append(row)
            save()
        snapshot("932000.CSI", "existing positive control")
        usCode = None
        for market in ("101", "102", "103"):
            for listType in (0, 1):
                row = {"category": market, "listType": listType, "startedAtUtc": utcNow()}
                row.update(decodeResponse(dll.GetStockListInStr(tq._get_run_id(), market.encode(), listType, 3000)))
                value = row.get("response", {}).get("Value")
                row["endedAtUtc"] = utcNow()
                if isinstance(value, list):
                    row["count"] = len(value)
                    row["matchingCrudeItems"] = [x for x in value if "CL00W" in str(x) or "原油" in str(x)]
                    if market == "103":
                        for item in value:
                            if isinstance(item, str) and item.endswith(".US"):
                                usCode = item
                                break
                evidence["enumerations"].append(row)
                save()
        if usCode:
            snapshot(usCode, "enumerated US equity positive control")
        snapshot("SCL8.SHF", "category 101 enumerated domestic crude main continuous; different instrument from NYMEX CL00W")
        candidates = list(dict.fromkeys([f"CL00W.{suffix}" for suffix in dllSuffixes] + ["CL00W.NYM", "CL00W.CME", "CL00W.ICE", "CL00W", "CL00W.17", "17#CL00W"]))
        for code in candidates[:24]:
            snapshot(code, "local identity or explicit installed suffix mapping; bounded diagnostic candidate")
        for row in list(evidence["snapshots"]):
            if row["classification"] == "quote_readable":
                time.sleep(1)
                snapshot(row["code"], "repeat readable control; request time is not upstream quote time")
    except (OSError, RuntimeError, ValueError, AttributeError) as exc:
        evidence["exception"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if initialized:
            tq.close()
            evidence["ownConnectionClosed"] = True
        evidence["endedAtUtc"] = utcNow()
        save()
    return 1 if "exception" in evidence else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tdx-root", type=Path, default=Path("D:/new_tdx"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=90, choices=range(20, 121), metavar="20..120")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return collectEvidence(args.tdx_root, args.output)
    try:
        result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker", "--tdx-root", str(args.tdx_root), "--output", str(args.output)], timeout=args.timeout, check=False)
        return result.returncode
    except subprocess.TimeoutExpired:
        print("probe timeout; only own subprocess terminated; partial evidence retained", file=sys.stderr)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())

# 代码更新时间：2026-10-01 13:37
