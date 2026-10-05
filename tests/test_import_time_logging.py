"""会被其它模块导入的脚本：宿主已配置日志时导入不得覆盖；单独导入时仍配置自己的同名日志。"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IMPORTED_SCRIPTS = {
    'opt_mins': 'opt_mins.log',
    'opt_vertical_spread_backtest': 'opt_vertical_spread_backtest.log',
    'fut_mapping': 'fut_mapping.log',
    'fut_trade_cal': 'fut_trade_cal.log',
    'fina_indicator': 'fina_indicator.log',
}


def _run(code: str, cwd) -> list[str]:
    return subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(cwd),
        env={**os.environ, 'PYTHONPATH': REPO_ROOT},
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    ).stdout.splitlines()


def _skipIfModuleMissing(module: str) -> None:
    if not os.path.isfile(os.path.join(REPO_ROOT, f'{module}.py')):
        pytest.skip(f'未纳入版本库：{module}.py')


@pytest.mark.parametrize('module', sorted(IMPORTED_SCRIPTS))
def test_host_logging_survives_import(module, tmp_path) -> None:
    _skipIfModuleMissing(module)
    out = _run(
        "import logging; logging.basicConfig(handlers=[logging.NullHandler()]);"
        f"import {module};"
        "print(sorted(type(h).__name__ for h in logging.getLogger().handlers))",
        tmp_path,
    )
    assert out[-1] == "['NullHandler']"
    assert not (tmp_path / IMPORTED_SCRIPTS[module]).exists()


@pytest.mark.parametrize('module', sorted(IMPORTED_SCRIPTS))
def test_standalone_import_uses_own_log(module, tmp_path) -> None:
    _skipIfModuleMissing(module)
    out = _run(
        f"import logging, {module};"
        "import os; print(sorted(os.path.basename(getattr(h, 'baseFilename', '')) for h in logging.getLogger().handlers))",
        tmp_path,
    )
    assert IMPORTED_SCRIPTS[module] in out[-1]

# 代码更新时间：2026-10-04 21:04 导入期日志不劫持宿主回归
# 代码更新时间：2026-10-05 10:11 模块文件不在仓库时 skip
