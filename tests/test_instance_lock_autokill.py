"""auto-kill：不得杀自身的 venv 转发器父进程；旧实例的子进程须一并结束。"""
from __future__ import annotations

import sys
import types

import pytest

from rt_monitor.core import instance_lock as il


class _FakeProc:
    def __init__(self, pid, cmdline, createTime, children=(), registry=None):
        self.pid = pid
        self.info = {'pid': pid, 'cmdline': cmdline, 'create_time': createTime}
        self._children = list(children)
        self.registry = registry

    def children(self, recursive=False):
        return [self.registry[c] for c in self._children]

    def kill(self):
        self.registry['killed'].append(self.pid)


def _install(monkeypatch, procs, me, parents):
    registry = {'killed': []}
    for p in procs:
        p.registry = registry
        registry[p.pid] = p

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return registry[self.pid].info['create_time']

        def parents(self):
            return [types.SimpleNamespace(pid=x) for x in parents]

    fake = types.SimpleNamespace(
        Process=_Proc,
        process_iter=lambda attrs: [p for p in procs],
        NoSuchProcess=type('NoSuchProcess', (Exception,), {}),
        AccessDenied=type('AccessDenied', (Exception,), {}),
        ZombieProcess=type('ZombieProcess', (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, 'psutil', fake)
    monkeypatch.setattr(il.os, 'getpid', lambda: me)
    monkeypatch.setattr(il.time, 'sleep', lambda s: None)
    monkeypatch.setenv('RT_MONITOR_AUTO_KILL_PREVIOUS', '1')
    return registry['killed']


APP = ['python.exe', 'D:\\x\\rt_monitor_app.py']


def test_skips_own_launcher_created_earlier(monkeypatch) -> None:
    procs = [
        _FakeProc(10, APP, 100.000),           # 本次启动的转发器（比自己早 50ms）
        _FakeProc(11, APP, 100.050),           # 自己
    ]
    killed = _install(monkeypatch, procs, me=11, parents=[10, 1])
    il.autoKillPreviousRtMonitorInstances(5000)
    assert killed == []


def test_kills_old_instance_with_children(monkeypatch) -> None:
    procs = [
        _FakeProc(1, APP, 50.0, children=[2, 3]),  # 旧转发器
        _FakeProc(2, APP, 50.01, children=[3]),    # 旧实例
        _FakeProc(3, ['python.exe', '-c', 'spawn_main'], 60.0),  # 旧 Ma 子进程
        _FakeProc(10, APP, 100.0),
        _FakeProc(11, APP, 100.05),
    ]
    killed = _install(monkeypatch, procs, me=11, parents=[10])
    il.autoKillPreviousRtMonitorInstances(5000)
    assert 3 in killed and 2 in killed and 1 in killed
    assert 10 not in killed and 11 not in killed


@pytest.mark.parametrize('flag', ['0', ''])
def test_disabled_flag_kills_nothing(monkeypatch, flag) -> None:
    procs = [_FakeProc(1, APP, 50.0), _FakeProc(11, APP, 100.0)]
    killed = _install(monkeypatch, procs, me=11, parents=[])
    monkeypatch.setenv('RT_MONITOR_AUTO_KILL_PREVIOUS', flag)
    il.autoKillPreviousRtMonitorInstances(5000)
    assert killed == []

# 代码更新时间：2026-10-04 17:42 auto-kill 排除祖先、连带子进程
