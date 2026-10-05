"""logger_config：默认行为不变；useDedicatedLogFile 单文件按天轮转；NO_FILE 时不写文件。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROBE = (
    "import json, logging, os, sys;"
    "sys.path.insert(0, os.getcwd());"
    "import logger_config as lc;"
    "mode = sys.argv[1];"
    "path = lc.useDedicatedLogFile('svc') if mode == 'dedicated' else '';"
    "logging.getLogger('t').info('hello-once');"
    "hs = logging.getLogger().handlers;"
    "print(json.dumps({'path': path, 'handlers': [[type(h).__name__, os.path.basename(getattr(h, 'baseFilename', '')),"
    " getattr(h, 'when', ''), getattr(h, 'backupCount', None)] for h in hs]}))"
)


def _run(tmp_path, mode: str, noFile: bool) -> dict:
    for dep in ('logger_config.py', 'windows_safe_log.py'):
        shutil.copy(os.path.join(REPO_ROOT, dep), tmp_path / dep)
    env = {k: v for k, v in os.environ.items() if k != 'LOGGER_CONFIG_NO_FILE'}
    if noFile:
        env['LOGGER_CONFIG_NO_FILE'] = '1'
    out = subprocess.run(
        [sys.executable, '-c', _PROBE, mode], cwd=str(tmp_path), env=env,
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip().splitlines()[-1]
    return json.loads(out)


def test_default_import_behavior_unchanged(tmp_path) -> None:
    res = _run(tmp_path, 'default', noFile=False)
    names = sorted(h[1] for h in res['handlers'] if h[1])
    assert names == ['error.log', 'performance.log', 'structured.log', '融资融券明细数据导入.log']


def test_dedicated_log_single_file_daily_rotation(tmp_path) -> None:
    res = _run(tmp_path, 'dedicated', noFile=False)
    fileHandlers = [h for h in res['handlers'] if h[1]]
    assert fileHandlers == [['WindowsSafeTimedRotatingFileHandler', 'svc.log', 'MIDNIGHT', 3]]
    assert os.path.isabs(res['path'])
    text = (tmp_path / 'logs' / 'svc.log').read_text(encoding='utf-8')
    assert text.count('hello-once') == 1
    for legacy in ('structured.log', 'performance.log'):
        legacyPath = tmp_path / 'logs' / legacy
        assert not legacyPath.exists() or 'hello-once' not in legacyPath.read_text(encoding='utf-8')


def test_no_file_env_writes_nothing(tmp_path) -> None:
    res = _run(tmp_path, 'dedicated', noFile=True)
    assert [h for h in res['handlers'] if h[1]] == []
    logDir = tmp_path / 'logs'
    assert not logDir.exists() or not any(p.stat().st_size for p in logDir.iterdir())

# 代码更新时间：2026-10-04 17:42 专用日志入口与默认行为测试
