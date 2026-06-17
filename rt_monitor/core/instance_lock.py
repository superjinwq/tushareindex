"""单实例锁与启动前清理旧 rt_monitor 进程（自 rt_monitor_app monolith 抽出）。"""
from __future__ import annotations

import atexit
import hashlib
import os
import re
import signal
import subprocess
import sys
import time

from logger_config import get_logger

from rt_monitor.core.config import APP_LOCK_FILE, REPO_ROOT, RT_MONITOR_LISTEN_PORT

logger = get_logger(__name__)

_appLockAcquired = False
_appWin32Mutex = None
_appPosixLockFd = None


def envFlag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    val = str(raw).strip().lower()
    if val in ('1', 'true', 'yes', 'on', 'y'):
        return True
    if val in ('0', 'false', 'no', 'off', 'n'):
        return False
    return default


def cmdlineForPid(pid: int) -> str:
    try:
        import psutil

        return ' '.join(psutil.Process(pid).cmdline() or [])
    except Exception:
        pass
    if sys.platform == 'win32' or os.name == 'nt':
        try:
            r = subprocess.run(
                [
                    'powershell',
                    '-NoProfile',
                    '-Command',
                    f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}").CommandLine',
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            return (r.stdout or '').strip()
        except Exception:
            return ''
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read().replace(b'\x00', b' ').decode('utf-8', errors='replace')
    except Exception:
        return ''


def listenPidsOnPortNetstat(port: int) -> set[int]:
    out: set[int] = set()
    try:
        kw: dict = {'capture_output': True, 'text': True, 'timeout': 20}
        if sys.platform == 'win32' or os.name == 'nt':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        r = subprocess.run(['netstat', '-ano'], **kw)
        for line in (r.stdout or '').splitlines():
            line = line.strip()
            if 'LISTEN' not in line.upper():
                continue
            if f':{port}' not in line:
                continue
            if not re.search(rf':{port}(?:\s|$)', line):
                continue
            parts = line.split()
            if parts and parts[-1].isdigit():
                out.add(int(parts[-1]))
    except Exception:
        pass
    return out


def killPidHard(pid: int) -> None:
    if sys.platform == 'win32' or os.name == 'nt':
        kw: dict = {'capture_output': True, 'timeout': 20}
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        subprocess.run(['taskkill', '/F', '/PID', str(pid)], **kw)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        time.sleep(0.25)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def autoKillPreviousRtMonitorInstances(listenPort: int | None = None) -> None:
    """结束本机其它 rt_monitor_app.py 进程。RT_MONITOR_AUTO_KILL_PREVIOUS=1 开启。"""
    if not envFlag('RT_MONITOR_AUTO_KILL_PREVIOUS', False):
        return
    port = listenPort if listenPort is not None else RT_MONITOR_LISTEN_PORT
    me = os.getpid()
    marker = 'rt_monitor_app.py'
    killed: list[int] = []
    try:
        import psutil

        try:
            meCreateTime = float(psutil.Process(me).create_time())
        except Exception:
            meCreateTime = 0.0

        for proc in psutil.process_iter(['pid', 'cmdline', 'create_time']):
            try:
                pid = int(proc.info.get('pid') or 0)
                if pid <= 0 or pid == me:
                    continue
                pCreateTime = float(proc.info.get('create_time') or 0.0)
                if meCreateTime > 0.0 and pCreateTime >= (meCreateTime - 0.01):
                    continue
                cl = proc.info.get('cmdline') or []
                s = ' '.join(cl)
                if marker.lower() not in s.lower():
                    continue
                if f' {me}' in s or s.strip().endswith(str(me)):
                    continue
                logger.info("自动结束旧 rt_monitor 进程 PID=%s（匹配命令行中的 %s）", pid, marker)
                proc.kill()
                killed.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except ImportError:
        for pid in sorted(listenPidsOnPortNetstat(port)):
            if pid == me:
                continue
            cmd = cmdlineForPid(pid)
            if marker.lower() not in cmd.lower():
                continue
            logger.info("自动结束占用端口 %s 的旧 rt_monitor 进程 PID=%s", port, pid)
            try:
                killPidHard(pid)
                killed.append(pid)
            except Exception as e:
                logger.warning("结束 PID %s 失败: %s", pid, e)
    if killed:
        time.sleep(0.6)
        logger.info("已请求结束 %s 个旧进程，继续本次启动。", len(killed))


def releaseSingleInstanceLock() -> None:
    global _appLockAcquired, _appWin32Mutex, _appPosixLockFd
    if not _appLockAcquired:
        return
    if sys.platform == 'win32' or os.name == 'nt':
        h = _appWin32Mutex
        _appWin32Mutex = None
        if h:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass
    else:
        fd = _appPosixLockFd
        _appPosixLockFd = None
        if fd is not None:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass
    try:
        if os.path.exists(APP_LOCK_FILE):
            os.remove(APP_LOCK_FILE)
    except Exception:
        pass
    _appLockAcquired = False


def acquireSingleInstanceLock() -> bool:
    global _appLockAcquired, _appWin32Mutex, _appPosixLockFd
    if _appLockAcquired:
        return True

    if sys.platform == 'win32' or os.name == 'nt':
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        kernel32.SetLastError(0)
        tag = hashlib.md5(os.path.normcase(REPO_ROOT).encode('utf-8'), usedforsecurity=False).hexdigest()[:12]
        mutexName = f'Local\\rt_monitor_app_{tag}'
        handle = kernel32.CreateMutexW(None, False, mutexName)
        if not handle:
            logger.error("CreateMutexW 失败，无法建立单实例锁。")
            return False
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            logger.error(
                "检测到已有实例运行（互斥体 %s），本次启动已拒绝。"
                "若已关闭旧窗口仍如此，请在任务管理器中结束残留的 python.exe 后再试。",
                mutexName,
            )
            return False
        _appWin32Mutex = handle
        _appLockAcquired = True
        atexit.register(releaseSingleInstanceLock)
        try:
            with open(APP_LOCK_FILE, 'w', encoding='utf-8') as f:
                f.write(f"{os.getpid()}|{int(time.time())}")
        except Exception:
            pass
        return True

    import fcntl

    try:
        fd = os.open(APP_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    except Exception as e:
        logger.error("打开锁文件失败 %s: %s", APP_LOCK_FILE, e)
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            os.close(fd)
        except Exception:
            pass
        logger.error("检测到已有实例运行（文件锁 %s），本次启动已拒绝。", APP_LOCK_FILE)
        return False
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}|{int(time.time())}".encode('utf-8'))
        os.fsync(fd)
    except Exception:
        pass
    _appPosixLockFd = fd
    _appLockAcquired = True
    atexit.register(releaseSingleInstanceLock)
    return True


def handleExitSignal(signum, _frame):
    logger.info("收到退出信号(%s)，准备释放单实例锁。", signum)
    releaseSingleInstanceLock()
    raise SystemExit(0)


def installWin32ConsoleCtrlHandler() -> None:
    if not (sys.platform == 'win32' or os.name == 'nt'):
        return
    try:
        import ctypes

        CTRL_C_EVENT = 0
        CTRL_BREAK_EVENT = 1
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        def consoleHandler(event):
            if event in (
                CTRL_CLOSE_EVENT,
                CTRL_LOGOFF_EVENT,
                CTRL_SHUTDOWN_EVENT,
                CTRL_C_EVENT,
                CTRL_BREAK_EVENT,
            ):
                releaseSingleInstanceLock()
            return 0

        ctypes.windll.kernel32.SetConsoleCtrlHandler(consoleHandler, True)
        installWin32ConsoleCtrlHandler._preventGc = consoleHandler
    except Exception:
        pass


def ensureSingleInstanceOrExit(listenPort: int | None = None) -> None:
    """启动前：可选 auto-kill + 单实例锁；失败则 SystemExit(1)。"""
    port = listenPort if listenPort is not None else RT_MONITOR_LISTEN_PORT
    autoKillPreviousRtMonitorInstances(port)
    if not envFlag('RT_MONITOR_SINGLE_INSTANCE', True):
        logger.warning("RT_MONITOR_SINGLE_INSTANCE=0，已关闭单实例锁（仅建议本地调试时使用）。")
        return
    if acquireSingleInstanceLock():
        return
    autoKillPreviousRtMonitorInstances(port)
    if acquireSingleInstanceLock():
        return
    sys.stderr.write(
        '[rt_monitor] 启动失败：单实例锁仍无法获取（可能仍有旧进程或权限不足）。\n'
        '请结束占用端口 %s 的 python，或安装 psutil 以便自动匹配 rt_monitor_app.py 进程。\n'
        '浏览器连旧进程会导致新接口 404；可设 RT_MONITOR_SINGLE_INSTANCE=0 仅本地调试。\n'
        % (port,)
    )
    raise SystemExit(1)
