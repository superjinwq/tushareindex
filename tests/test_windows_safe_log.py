"""windows_safe_log：轮转 rename 失败不抛异常、继续写原文件；成功时正常轮转；各脚本引用的实现不被换错。"""
from __future__ import annotations

import ast
import errno
import logging
import os
from pathlib import Path

import pytest

import windows_safe_log as wsl

REPO_ROOT = Path(__file__).resolve().parent.parent

ALL_CLASSES = [
    wsl.WindowsSafeTimedRotatingFileHandler,
    wsl.RetryTimedRotatingFileHandler,
    wsl.ManualRenameTimedRotatingFileHandler,
]


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord('t', logging.INFO, __file__, 1, msg, None, None)


def _lockedError() -> PermissionError:
    e = PermissionError(errno.EACCES, '另一个程序正在使用此文件')
    e.winerror = 32
    return e


@pytest.fixture(autouse=True)
def _noSleep(monkeypatch):
    monkeypatch.setattr(wsl.time, 'sleep', lambda s: None)


@pytest.mark.parametrize('cls', ALL_CLASSES, ids=lambda c: c.__name__)
def test_rollover_rename_locked_keeps_writing_original(tmp_path, monkeypatch, cls) -> None:
    logPath = tmp_path / 'x.log'
    h = cls(str(logPath), when='midnight', backupCount=3, encoding='utf-8')
    try:
        h.emit(_record('before'))
        calls = []

        def lockedRename(src, dst):
            calls.append((src, dst))
            raise _lockedError()

        monkeypatch.setattr(os, 'rename', lockedRename)
        h.doRollover()
        h.emit(_record('after'))
        h.flush()
        assert len(calls) == 3
        text = logPath.read_text(encoding='utf-8')
        assert 'before' in text and 'after' in text
        assert [p.name for p in tmp_path.iterdir()] == ['x.log']
    finally:
        h.close()


@pytest.mark.parametrize('cls', ALL_CLASSES, ids=lambda c: c.__name__)
def test_rollover_success_creates_backup(tmp_path, cls) -> None:
    logPath = tmp_path / 'x.log'
    h = cls(str(logPath), when='midnight', backupCount=3, encoding='utf-8')
    try:
        h.emit(_record('old-day'))
        h.doRollover()
        h.emit(_record('new-day'))
        h.flush()
        names = sorted(p.name for p in tmp_path.iterdir())
        assert len(names) == 2 and names[0] == 'x.log' and names[1].startswith('x.log.')
        assert 'new-day' in logPath.read_text(encoding='utf-8')
        assert 'old-day' in (tmp_path / names[1]).read_text(encoding='utf-8')
    finally:
        h.close()


def test_manual_rename_reraises_non_lock_errors(tmp_path, monkeypatch) -> None:
    """原实现：非占用类 OSError 照常抛出（保持与原脚本副本一致，不被统一时顺手吞掉）。"""
    h = wsl.ManualRenameTimedRotatingFileHandler(str(tmp_path / 'x.log'), when='midnight', backupCount=3)
    try:
        h.emit(_record('a'))

        def badRename(src, dst):
            raise OSError(errno.ENOSPC, 'disk full')

        monkeypatch.setattr(os, 'rename', badRename)
        with pytest.raises(OSError):
            h.doRollover()
    finally:
        h.close()


def _normalizedClassHash(node: ast.ClassDef) -> str:
    import hashlib

    n = ast.parse(ast.unparse(node)).body[0]
    n.name = 'X'
    n.bases = [ast.Name('Base')]
    for fn in ast.walk(n):
        if (isinstance(fn, (ast.FunctionDef, ast.ClassDef)) and fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(getattr(fn.body[0], 'value', None), ast.Constant)
                and isinstance(fn.body[0].value.value, str)):
            fn.body = fn.body[1:] or [ast.Pass()]
    return hashlib.sha1(ast.unparse(n).encode()).hexdigest()[:8]


def test_shared_classes_identical_to_original_copies() -> None:
    """去注释/文档字符串后的语法树哈希 = 统一前各脚本副本的哈希（2026-10-04 实测分组）。"""
    tree = ast.parse((REPO_ROOT / 'windows_safe_log.py').read_text(encoding='utf-8'))
    got = {n.name: _normalizedClassHash(n) for n in tree.body if isinstance(n, ast.ClassDef)}
    assert got == {
        'WindowsSafeTimedRotatingFileHandler': '230d946d',
        'RetryTimedRotatingFileHandler': '9f3a35cb',
        'ManualRenameTimedRotatingFileHandler': '1f0d68cd',
    }


_EXPECTED_BINDINGS = {
    'WindowsSafeTimedRotatingFileHandler': [
        'etf宽基盘后数据入库.py', 'fut_basic.py', 'fut_mapping.py', 'fut_daily.py', 'fut_trade_cal.py',
        'index_daily.py', 'index_sw_daily.py', 'logger_config.py', 'margin_sum.py', 'opt_daily.py',
        'opt_basic.py', 'opt_vertical_spread_backtest.py', 'opt_mins.py', 'rt_sw_k.py',
        'tdx_vs_qmt_159915_probe.py', 'tdx_opt_live.py',
    ],
    'RetryTimedRotatingFileHandler': ['limit_list_ths.py', 'report_rc.py', 'stk_surv.py'],
    'ManualRenameTimedRotatingFileHandler': [
        'marketinfo_from_stk_factor_pro.py', 'test_prev5_close_vs_hl.py', '盘中实时统计.py', 'tusharert个股实时行情.py',
    ],
}


@pytest.mark.parametrize(
    'script,shared',
    [(s, c) for c, ss in _EXPECTED_BINDINGS.items() for s in ss],
    ids=lambda v: str(v),
)
def test_script_binds_original_variant(script, shared) -> None:
    """每个脚本里的 WindowsSafeTimedRotatingFileHandler 必须指向与原副本等价的那一种实现。"""
    tree = ast.parse((REPO_ROOT / script).read_text(encoding='utf-8'))
    assert not any(isinstance(n, ast.ClassDef) and n.name == 'WindowsSafeTimedRotatingFileHandler' for n in tree.body)
    bound = {
        (a.asname or a.name): a.name
        for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == 'windows_safe_log'
        for a in n.names
    }
    assert bound.get('WindowsSafeTimedRotatingFileHandler') == shared

# 代码更新时间：2026-10-04 18:38 共享 Windows 安全轮转 Handler 回归
