"""RT_MONITOR_STATIC_VER：static/+templates/ 内容哈希，确定、随内容变化、环境变量可覆盖。"""
from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.core import config  # noqa: E402


def _makeRepo(tmp_path):
    (tmp_path / 'static' / 'js').mkdir(parents=True)
    (tmp_path / 'templates').mkdir()
    (tmp_path / 'static' / 'js' / 'a.js').write_bytes(b'var a = 1;')
    (tmp_path / 'templates' / 'p.html').write_bytes(b'<script>var b = 2;</script>')
    return tmp_path


def test_deterministic_and_content_sensitive(tmp_path) -> None:
    repo = str(_makeRepo(tmp_path))
    v1 = config.computeRtMonitorStaticVer(repo)
    assert v1 == config.computeRtMonitorStaticVer(repo)
    assert v1.startswith('c') and len(v1) == 11
    (tmp_path / 'templates' / 'p.html').write_bytes(b'<script>var b = 3;</script>')
    v2 = config.computeRtMonitorStaticVer(repo)
    assert v2 != v1
    (tmp_path / 'static' / 'js' / 'new.js').write_bytes(b'')
    assert config.computeRtMonitorStaticVer(repo) != v2


def test_module_value_matches_repo_content_without_override() -> None:
    if os.getenv('RT_MONITOR_STATIC_VER', '').strip():
        return
    assert config.RT_MONITOR_STATIC_VER == config.computeRtMonitorStaticVer()


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv('RT_MONITOR_STATIC_VER', 'manual-1')
    try:
        assert importlib.reload(config).RT_MONITOR_STATIC_VER == 'manual-1'
    finally:
        monkeypatch.delenv('RT_MONITOR_STATIC_VER')
        importlib.reload(config)

# 代码更新时间：2026-10-04 21:47 新建：静态版本号内容哈希回归
