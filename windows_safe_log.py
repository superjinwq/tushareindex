"""Windows 安全的按时间轮转日志 Handler（共享实现，无模块级副作用）。

Windows 下日志文件被其它进程占用时 rename 会失败；以下三种实现分别从各脚本原样抽出，
行为与原副本逐字等价，按原副本所属分组引用，不得互相替换：

- WindowsSafeTimedRotatingFileHandler：PermissionError 重试 3 次（间隔 0.3s），仍失败则放弃本轮、重开原文件继续写。
- RetryTimedRotatingFileHandler：OSError 按 0/0.2/0.4s 重试，仍失败静默放弃本轮（下次写入再试）。
- ManualRenameTimedRotatingFileHandler：手写 rename 流程，被占用(EACCES/WinError 32)重试 3 次，失败不抛、照常推进下次轮转时刻；其它 OSError 照常抛出。
"""
from __future__ import annotations

import errno
import os
import time
from logging.handlers import TimedRotatingFileHandler


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 下 rename 可能失败：重试后仍失败则跳过本轮轮转，继续写原文件。"""

    def doRollover(self) -> None:  # noqa: N802
        for _ in range(3):
            try:
                super().doRollover()
                return
            except PermissionError:
                time.sleep(0.3)
        if self.stream:
            try:
                self.stream.close()
            except Exception:
                pass
        self.stream = self._open()


class RetryTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 下 rename 可能失败：短暂重试后放弃本轮轮转，继续写原文件。"""

    def doRollover(self) -> None:  # noqa: N802
        for delay in (0, 0.2, 0.4):
            if delay:
                time.sleep(delay)
            try:
                super().doRollover()
                return
            except OSError:
                continue


class ManualRenameTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 下轮转时文件被占用：重试并不抛异常，避免 PermissionError 刷屏。"""

    def doRollover(self) -> None:  # noqa: N802
        if self.stream:
            self.stream.close()
            self.stream = None

        currentTime = int(time.time())
        t = self.rolloverAt - self.interval
        if self.utc:
            timeTuple = time.gmtime(t)
        else:
            timeTuple = time.localtime(t)

        dfn = self.rotation_filename(self.baseFilename + "." + time.strftime(self.suffix, timeTuple))

        if os.path.exists(dfn):
            try:
                os.remove(dfn)
            except OSError:
                pass

        for attempt in range(3):
            try:
                if os.path.exists(self.baseFilename):
                    os.rename(self.baseFilename, dfn)
                break
            except OSError as e:
                if e.errno == errno.EACCES or (getattr(e, "winerror", None) == 32):
                    if attempt < 2:
                        time.sleep(0.2 * (attempt + 1))
                    continue
                raise

        if self.backupCount > 0:
            for s in self.getFilesToDelete():
                try:
                    os.remove(s)
                except OSError:
                    pass

        if not self.delay:
            self.stream = self._open()

        nextRolloverAt = self.computeRollover(currentTime)
        while nextRolloverAt <= currentTime:
            nextRolloverAt += self.interval
        self.rolloverAt = nextRolloverAt

# 代码更新时间：2026-10-04 18:38 从 22 个脚本副本抽出三种 Windows 安全轮转实现
