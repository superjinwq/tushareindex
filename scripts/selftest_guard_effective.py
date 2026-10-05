# coding: utf-8
"""故障注入：临时破坏一条守卫，验证自检器真的会 FAIL（守卫有效性自检）。

做法：把 `大QMT实时行情.py` 里 `(refMs - ms)` 临时改回 `abs(refMs - ms)`，
跑 `selfcheck_delivery.py --only lessons`，确认 L001 报 FAIL，然后**原样还原**。
全程校验文件哈希，确保还原无损。

用法：
    python -X utf8 scripts/selftest_guard_effective.py

注意：本脚本会短暂改写源码做故障注入，必须与 selfcheck_delivery.py
及其他读取工作树的验收串行执行，不得放入并行验收批次。
"""
from __future__ import annotations
# 用途: 守卫
# 保留至: 2026-10-20

import hashlib
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(_REPO, "大QMT实时行情.py")
NEEDLE = "if refMs > 0 and (refMs - ms) > float(maxAgeSec) * 1000.0:"
BROKEN = "if refMs > 0 and abs(refMs - ms) > float(maxAgeSec) * 1000.0:"


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def runCheck() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "scripts/selfcheck_delivery.py", "--only", "lessons"],
        cwd=_REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    before = sha(TARGET)
    # 必须用**二进制**读写：文本模式会把 \r\n 规范化成 \n，导致"还原后哈希不一致"。
    with open(TARGET, "rb") as fh:
        raw = fh.read()
    src = raw.decode("utf-8")
    if NEEDLE not in src:
        print(f"✗ 未找到注入点，无法测试：{NEEDLE}")
        return 2

    rc0, out0 = runCheck()
    print(f"[基线] 退出码={rc0}  L001 行="
          f"{[ln.strip() for ln in out0.splitlines() if 'L001' in ln]}")

    try:
        broken = src.replace(NEEDLE, BROKEN, 1).encode("utf-8")
        with open(TARGET, "wb") as fh:
            fh.write(broken)
        rc1, out1 = runCheck()
        l001 = [ln.strip() for ln in out1.splitlines() if "L001" in ln]
        print(f"[注入] 退出码={rc1}  L001 行={l001}")
    finally:
        with open(TARGET, "wb") as fh:
            fh.write(raw)
        after = sha(TARGET)

    ok = (rc0 == 0) and (rc1 != 0) and ("✗" in "".join(l001))
    print(f"\n还原校验：{'✓ 哈希一致' if before == after else '✗ 哈希不一致!'}")
    print(f"守卫有效性：{'✓ 注入后确实 FAIL（守卫有效）' if ok else '✗ 守卫未生效'}")
    return 0 if (ok and before == after) else 1


if __name__ == "__main__":
    raise SystemExit(main())
