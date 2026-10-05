"""回测脚本被 rt_monitor 导入时不得劫持宿主日志；单独运行时仍自建日志。"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import opt_vertical_spread_backtest as bt  # noqa: E402


def test_setup_logger_keeps_host_handlers() -> None:
    root = logging.getLogger()
    hostHandler = logging.NullHandler()
    root.addHandler(hostHandler)
    try:
        before = list(root.handlers)
        bt.setupLogger()
        assert root.handlers == before
    finally:
        root.removeHandler(hostHandler)


def test_standalone_import_configures_own_log(tmp_path) -> None:
    code = (
        "import logging, opt_vertical_spread_backtest as bt;"
        "hs = logging.getLogger().handlers;"
        "print(sorted(type(h).__name__ for h in hs));"
        "print(any(getattr(h, 'baseFilename', '').endswith(bt.LOG_FILE) for h in hs))"
    )
    out = subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(tmp_path),
        env={**os.environ, 'PYTHONPATH': REPO_ROOT},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout.splitlines()
    assert out[-2] == "['StreamHandler', 'WindowsSafeTimedRotatingFileHandler']"
    assert out[-1] == 'True'

# 代码更新时间：2026-10-04 17:42 独立运行子进程改用临时 cwd
