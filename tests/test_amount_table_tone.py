# coding: utf-8
"""成交额表着色规则：直接执行 static/js/rt_monitor.js 中的函数。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def testAmountTableToneJs():
    root = Path(__file__).resolve().parents[1]
    script = root / "tests" / "test_amount_table_tone.js"
    proc = subprocess.run(
        ["node", str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "ok" in (proc.stdout or "")


if __name__ == "__main__":
    sys.exit(subprocess.call(["node", str(Path(__file__).with_name("test_amount_table_tone.js"))]))
