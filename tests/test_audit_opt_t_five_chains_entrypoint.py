"""五链审计脚本应可从任意工作目录直接启动。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_opt_t_five_chains.py"


def testAuditEntrypointBootstrapsRepoRoot(tmp_path: Path) -> None:
    """不能依赖调用方预先设置 PYTHONPATH。"""
    probe = """
import importlib.util
import sys
from pathlib import Path

script = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('auditOptTBoardProbe', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert Path(module.REPO_ROOT).resolve() == script.parents[1].resolve()
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", probe, str(SCRIPT_PATH)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
