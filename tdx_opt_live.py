# coding: utf-8
"""通达信期权实时喂数：主链 159915+510050；可选慢刷兜底 510300/510500/588000；另写中证2000/北证50 → rt_idx_k。

独立进程，不进大 QMT 主循环。主链快照约每秒；三链兜底默认约 1.0s 一轮（不拖主链）。
**周期强刷默认关闭**（`TDX_OPT_REFRESH_SESSION_SEC=0`）：`refresh_cache(force=True)`
会让通达信弹数据刷新进度条，实测对新鲜度无实质贡献（A/B 见常量处注释），
目前只在五档缺失 / 覆盖率过低 / P75 滞后超阈值时才补刷。
HTTP 供大 QMT 合并期权；指数两行写入 stock_tushare.rt_idx_k。

  python tdx_opt_live.py
  默认 http://127.0.0.1:15581/snapshot
  关三链兜底：TDX_OPT_FALLBACK_ENABLE=0
  运行时可调周期强刷：http://127.0.0.1:15581/set_refresh_sec?sec=3（0=关）
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pymysql

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = str(SCRIPT_DIR / "tdx_opt_live.log")
TDX_USER_DIR = Path(r"D:\new_tdx\PYPlugins\user")
HTTP_HOST = "127.0.0.1"
HTTP_PORT = int(os.getenv("TDX_OPT_HTTP_PORT", "15581"))
# 强制刷缓存：默认仅健康按需；保底默认关闭（0）。
# TDX_OPT_REFRESH_SAFETY_SEC>0 或旧名 TDX_OPT_REFRESH_SEC>0 时启用保底秒数。
def _envRefreshSafetySec() -> float:
    raw = os.getenv("TDX_OPT_REFRESH_SAFETY_SEC")
    if raw is None or str(raw).strip() == "":
        raw = os.getenv("TDX_OPT_REFRESH_SEC")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


REFRESH_SAFETY_SEC = _envRefreshSafetySec()
# 盘中周期性真刷间隔（秒）。**2026-09-14 用户决定：恢复为 3（方案 A）**。
#
# 决策依据（都是实测，不是推断）：
#   1. TDX 本地缓存**完全不会自更新**。决定性排除实验
#      （`scripts/probe_tdx_selfupdate_offline.py`）：关掉周期刷 + 保险丝阈值临时设
#      3600s（观察窗内不可能触发）后，**420 秒内时间戳推进 0 次**，
#      滞后从 8.8s 单调涨到 409.2s。
#   2. 所以 `refresh_cache(force=True)` 是**唯一**能让缓存前进的手段 ——
#      此前我记录的"自然脉冲周期 33s→135s"是**误判**：把每次"脉冲"时刻与
#      日志 `refresh_cache ok` 对齐后一一对应，那些脉冲全是我自己的保险丝触发的
#      （`stale_p75` ≈240s 的自我实现循环，见 LESSON-017）。
#   3. 由此："关闭刷新"与"保持新鲜"**不可兼得**，只能选刷新间隔。
#      用户权衡后选择新鲜度优先。
#
# ⚠️ **代价（用户已知悉并接受）**：每次强刷都会让通达信弹"数据刷新"进度条。
#    3s 间隔 ≈ 盘中持续弹（09-11 实测当天 3116 次、99.2% 来自定时器）。
#    要改成别的节奏，不必改代码：
#      `curl "http://127.0.0.1:15581/set_refresh_sec?sec=N"`（0 = 关闭）
#    设 TDX_OPT_REFRESH_SESSION_SEC=0 或运行时 sec=0 可随时退回"不弹框但要旧数据"。
# 另有兜底（默认不会触发，因为周期刷已在跑）：P75 滞后 ≥ REFRESH_STALE_P75_SEC
# 连续 STALE_LOW_STREAK 轮、或五档缺失/覆盖率过低时补刷（见 decideForceRefresh）。
REFRESH_SESSION_SEC = float(os.getenv("TDX_OPT_REFRESH_SESSION_SEC", "3"))
# 两次强刷最小间隔；应 <= REFRESH_SESSION_SEC，否则周期刷新被冷却吃掉。
REFRESH_COOLDOWN_SEC = float(os.getenv("TDX_OPT_REFRESH_COOLDOWN_SEC", "1"))
# 启动是否强制暖缓存（默认开）；设 TDX_OPT_REFRESH_ON_STARTUP=0 可关
REFRESH_ON_STARTUP = os.getenv("TDX_OPT_REFRESH_ON_STARTUP", "1") == "1"
# 五档健康：曾达到 listed*ARM 后，若 five < peak*DROP 连续 BAD_STREAK 轮则强刷
FIVE_PEAK_ARM_RATIO = float(os.getenv("TDX_OPT_FIVE_ARM_RATIO", "0.8"))
FIVE_DROP_RATIO = float(os.getenv("TDX_OPT_FIVE_DROP_RATIO", "0.5"))
HEALTH_BAD_STREAK = int(os.getenv("TDX_OPT_HEALTH_BAD_STREAK", "3"))
SNAP_ERR_RATIO = float(os.getenv("TDX_OPT_SNAP_ERR_RATIO", "0.35"))
# 主链五档覆盖过低：不依赖 peak（竞价薄盘曾把 peak 锁在十档导致永不 health_bad）
COVERAGE_MIN_RATIO = float(os.getenv("TDX_OPT_COVERAGE_MIN_RATIO", "0.35"))
COVERAGE_LOW_STREAK = int(os.getenv("TDX_OPT_COVERAGE_LOW_STREAK", "3"))
# 陈旧度保险丝（配合"关闭周期强刷"）：快照报价时间戳滞后 P75 ≥ 该秒数，
# 连续 STALE_LOW_STREAK 轮 → 补一次真刷。
# ⚠️ 阈值必须**明显大于实测锯齿峰值**，否则保险丝退化成周期性弹框源。
# 两次实测（同一条教训重复了一次，所以把数字写死在这里）：
#   2026-09-11：锯齿周期 ≈33s、峰值 ≈33s（当时取 120s 尚有余量）
#   2026-09-14：锯齿周期 ≈135s、峰值 ≈135s（周期变慢）→ **120s 阈值每周期都触发**，
#               盘中约每 2 分钟弹一次框（当天 stale_p75 触发 59 次）
# 现取 240s ≈ 1.8× 实测峰值，再叠加最小间隔双保险：
# 设 TDX_OPT_STALE_P75_SEC=0 可完全关闭该保险丝。
REFRESH_STALE_P75_SEC = float(os.getenv("TDX_OPT_STALE_P75_SEC", "240"))
STALE_LOW_STREAK = int(os.getenv("TDX_OPT_STALE_LOW_STREAK", "2"))
# 实测锯齿峰值（秒）：标定值，供守卫断言"阈值必须大于它"。
# 更新方法：跑 scripts/probe_opt_refresh_throughput.py 与
# scripts/probe_tdx_cache_progress.py，取观测到的最大锯齿峰值。
OBSERVED_SAWTOOTH_PEAK_SEC = float(os.getenv("TDX_OPT_SAWTOOTH_PEAK_SEC", "135"))
# 陈旧补刷之间的最小间隔：防止"阈值贴近峰值"时退化成周期性刷新（双保险）。
STALE_REFRESH_MIN_GAP_SEC = float(os.getenv("TDX_OPT_STALE_MIN_GAP_SEC", "30"))
# 指数写 rt_idx_k 节流（秒）；与期权共用 TQ，不另开 force refresh。
IDX_DB_WRITE_SEC = float(os.getenv("TDX_IDX_DB_WRITE_SEC", "2"))
# TQ 初始化：客户端晚开/插件未就绪时重试，避免 bat 秒退。
TDX_INIT_RETRY_SEC = float(os.getenv("TDX_INIT_RETRY_SEC", "180"))
TDX_INIT_RETRY_INTERVAL = float(os.getenv("TDX_INIT_RETRY_INTERVAL", "3"))
INSTANCE_LOCK_NAME = "tdx_opt_live.lock"
INSTANCE_MUTEX_NAME = r"Local\tdx_opt_live"
# bat 默认开：启动前杀掉其它 tdx_opt_live，避免 15581 双听。
AUTO_KILL_PREVIOUS = os.getenv("TDX_OPT_AUTO_KILL_PREVIOUS", "0") == "1"
# 主链：与 50/创业板同频快刷（QMT 盘口弱或盘后仍可用）
PRIMARY_CHAINS = (
    ("510050", "OP510050.SH"),
    ("159915", "OP159915.SZ"),
)
# 三链：慢刷缺口兜底，默认开；勿与主链同秒全量猛拉
FALLBACK_CHAINS = (
    ("510300", "OP510300.SH"),
    ("510500", "OP510500.SH"),
    ("588000", "OP588000.SH"),
)
FALLBACK_ENABLE = os.getenv("TDX_OPT_FALLBACK_ENABLE", "1") != "0"
FALLBACK_SEC = float(os.getenv("TDX_OPT_FALLBACK_SEC", "1.0"))
# 并行取数：主链（强刷+取数）与兜底取数互不依赖，并行可把单轮从 ~4.5s 压到 ~2.8s，
# 直接降低整链滞后上界。本机 64C/128T，余量充足。设 0 回退串行。
PARALLEL_PULL = os.getenv("TDX_OPT_PARALLEL_PULL", "1") != "0"
# 盘中探针：get_market_data(VolInStock) 验证 TQ 能否补仓；0=关闭。默认 90s。
VIS_PROBE_SEC = float(os.getenv("TDX_OPT_VIS_PROBE_SEC", "90"))
VIS_PROBE_N = max(1, int(os.getenv("TDX_OPT_VIS_PROBE_N", "3")))
# 兼容旧名
CHAINS = PRIMARY_CHAINS + (FALLBACK_CHAINS if FALLBACK_ENABLE else ())
# Tushare/QMT 无可靠实时源；TQ 码与首页 CATEGORY 一致（非 000852 中证1000）。
IDX_TQ_SPECS = (
    ("932000.CSI", "中证2000"),
    ("899050.BJ", "北证50"),
)
RT_IDX_K_TABLE = "rt_idx_k"
running = True
_snapLock = threading.Lock()
_snapJson = b'{"ok":false,"opt":{},"optN":0,"idx":{},"idxN":0}'
_lastRefreshMono = 0.0
_lastIdxDbMono = 0.0
_lastFallbackMono = 0.0
_fallbackBag: dict = {}
_lastOptBag: dict = {}
_lastSessionRefreshKey = ""
# 上一次「槽位刷新尝试」的时刻：仅用于冷却重试节流，不用于判定槽位是否已刷。
_lastSessionRefreshTryMono = 0.0
_peakFive = 0
_healthBadStreak = 0
_coverageLowStreak = 0
_staleLowStreak = 0
_healthSessionDate = None
_lastRawFieldNames: list[str] | None = None
_refreshRequested = False
_refreshRequestReason = ""
_visProbeRequested = False
_lastVisProbeMono = 0.0
_lastVisProbeResult: dict | None = None
_instanceLockFile = None
_instanceMutexHandle = None
# 兼容旧日志字段名
REFRESH_SEC = REFRESH_SAFETY_SEC


def resetHealthForNewSession(now: datetime | None = None) -> bool:
    """每个交易日重置 peak/连续计数，避免隔夜薄盘峰值锁死今日强刷。"""
    global _peakFive, _healthBadStreak, _coverageLowStreak, _healthSessionDate
    wall = now or datetime.now()
    day = wall.date()
    if _healthSessionDate == day:
        return False
    _healthSessionDate = day
    _peakFive = 0
    _healthBadStreak = 0
    _coverageLowStreak = 0
    return True


def dbConfig() -> dict:
    dbPassword = os.getenv("DB_PASSWORD", "").strip()
    if not dbPassword:
        raise SystemExit("缺少环境变量 DB_PASSWORD")
    return dict(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=dbPassword,
        database=os.getenv("DB_NAME", "stock_tushare"),
        charset="utf8mb4",
    )


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


def setupLogger() -> logging.Logger:
    logger = logging.getLogger("tdxOptLive")
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = WindowsSafeTimedRotatingFileHandler(
        LOG_FILE, when="D", interval=1, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.propagate = False
    return logger


def isTdxwRunning() -> bool:
    """本机是否已有通达信主程序 tdxw.exe。"""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq tdxw.exe"],
            capture_output=True,
            timeout=15,
            creationflags=flags,
        )
        text = (proc.stdout or b"") + (proc.stderr or b"")
        try:
            out = text.decode("gbk", errors="ignore")
        except Exception:
            out = text.decode("utf-8", errors="ignore")
        return "tdxw.exe" in out.lower()
    except Exception:
        return False


def connectTdx(logger):
    """连接 TQ。单实例由互斥体保证；TQ 策略路径用「脚本.pid{本PID}」唯一化，
    避免沿用裸脚本名与历史残留同名策略冲突（从而少打 try=1 WARNING）。"""
    if str(TDX_USER_DIR) not in sys.path:
        sys.path.insert(0, str(TDX_USER_DIR))
    from tqcenter import tq  # noqa: E402

    base = str(Path(__file__).resolve())
    # 仅用本进程唯一 path；不再先撞裸 tdx_opt_live.py
    path = f"{base}.pid{os.getpid()}"
    deadline = time.monotonic() + max(0.0, TDX_INIT_RETRY_SEC)
    attempt = 0
    lastExc: Exception | None = None

    while running:
        attempt += 1
        if not isTdxwRunning():
            remain = max(0.0, deadline - time.monotonic())
            logger.warning(
                "未检测到 tdxw.exe，请先开通达信（第 %d 次，约剩 %.0fs）",
                attempt,
                remain,
            )
        else:
            try:
                tq.initialize(path)
                logger.info(
                    "tdx init_ok path=%s run_id=%s attempt=%d",
                    path,
                    getattr(tq, "run_id", None),
                    attempt,
                )
                return tq
            except Exception as exc:
                lastExc = exc
                logger.warning("tdx init 失败 attempt=%d %s", attempt, exc)
                try:
                    tq.close()
                except Exception:
                    pass
            remain = max(0.0, deadline - time.monotonic())
            logger.warning(
                "TQ 暂不可用（常见：客户端刚开未就绪），%.0fs 内重试",
                remain,
            )

        if time.monotonic() >= deadline:
            break
        interruptibleSleep(TDX_INIT_RETRY_INTERVAL)

    raise RuntimeError(
        f"通达信 TQ 初始化失败（已重试约 {TDX_INIT_RETRY_SEC:.0f}s）: {lastExc}；"
        "请确认已打开 tdxw.exe，并结束其它占用 TQ 的 python 后再启 run_tdx_opt_live.bat"
    )


def interruptibleSleep(sec: float) -> None:
    """可中断短睡。remain 须在 sleep 前再取一次并夹到 >=0，避免 while 条件与
    sleep 参数之间时钟前移导致 ValueError: sleep length must be non-negative。"""
    end = time.monotonic() + max(0.0, float(sec))
    while running:
        remain = end - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(0.2, remain))


def defaultTdxOptLockPath() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(SCRIPT_DIR)
    folder = os.path.join(base, "tdx_opt_live")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, INSTANCE_LOCK_NAME)


def isTdxOptScriptCmd(cmd: str) -> bool:
    text = (cmd or "").lower().replace("/", "\\")
    return "tdx_opt_live.py" in text


def listOtherTdxOptPids() -> list[int]:
    """枚举其它正在跑 tdx_opt_live.py 的 python/pythonw 进程（不含本 PID）。"""
    me = os.getpid()
    out: list[int] = []
    if os.name != "nt":
        return out
    try:
        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'tdx_opt_live\\.py' } | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return out
    text = (raw or "").strip()
    if not text:
        return out
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return out
    if isinstance(data, dict):
        data = [data]
    for row in data or []:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid == me:
            continue
        if isTdxOptScriptCmd(str(row.get("CommandLine") or "")):
            out.append(pid)
    return out


def killOtherTdxOptPids(logger) -> list[int]:
    """最多两轮强杀其它实例，并等待进程表清空，避免双听 15581 / 双刷 TQ。"""
    killedAll: list[int] = []
    for roundIdx in range(2):
        pids = listOtherTdxOptPids()
        if not pids:
            break
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if pid not in killedAll:
                    killedAll.append(pid)
                logger.warning("已结束其它 tdx_opt_live pid=%s (round=%d)", pid, roundIdx + 1)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.error("结束 pid=%s 失败 %s", pid, exc)
        # 等进程退出 + 通达信侧释放旧策略句柄
        time.sleep(1.5)
    left = listOtherTdxOptPids()
    if left:
        logger.error("强杀后仍残留 tdx_opt_live PID=%s", ",".join(str(p) for p in left))
    return killedAll


def tryAcquireTdxOptLock(lockPath: str):
    lockFile = open(lockPath, "a+b")
    try:
        lockFile.seek(0, os.SEEK_END)
        if lockFile.tell() == 0:
            lockFile.write(b"\0")
            lockFile.flush()
        lockFile.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lockFile.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lockFile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lockFile.close()
        return None
    try:
        lockFile.seek(0)
        lockFile.truncate(0)
        lockFile.write(("%d\n" % os.getpid()).encode("ascii"))
        lockFile.flush()
    except OSError:
        pass
    return lockFile


def tryAcquireTdxOptMutex():
    if os.name != "nt":
        return True, None
    import ctypes

    kernel32 = ctypes.windll.kernel32
    errorAlreadyExists = 183
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, True, INSTANCE_MUTEX_NAME)
    if not handle:
        return False, None
    if kernel32.GetLastError() == errorAlreadyExists:
        kernel32.CloseHandle(handle)
        return False, None
    return True, handle


def releaseTdxOptSingleton() -> None:
    global _instanceLockFile, _instanceMutexHandle
    lockFile = _instanceLockFile
    _instanceLockFile = None
    if lockFile is not None:
        try:
            lockFile.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lockFile.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lockFile.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lockFile.close()
        except OSError:
            pass
    handle = _instanceMutexHandle
    _instanceMutexHandle = None
    if handle is not None and os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.ReleaseMutex(handle)
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def acquireTdxOptSingleton(logger) -> bool:
    """本机只允许一份 tdx_opt_live（进程清扫 + 互斥体 + 锁文件）。"""
    global _instanceLockFile, _instanceMutexHandle
    if AUTO_KILL_PREVIOUS:
        killed = killOtherTdxOptPids(logger)
        if killed:
            logger.info("单实例：已清理旧进程 %s", ",".join(str(p) for p in killed))
    others = listOtherTdxOptPids()
    if others:
        logger.error(
            "另一 tdx_opt_live 仍在运行 PID=%s。请先结束或设 TDX_OPT_AUTO_KILL_PREVIOUS=1。",
            ",".join(str(pid) for pid in others),
        )
        return False
    ok, mutexHandle = tryAcquireTdxOptMutex()
    if not ok:
        logger.error(
            "另一 tdx_opt_live 已在运行（互斥体 %s）。同时只允许一份。",
            INSTANCE_MUTEX_NAME,
        )
        return False
    _instanceMutexHandle = mutexHandle
    lockPath = defaultTdxOptLockPath()
    lockFile = tryAcquireTdxOptLock(lockPath)
    if lockFile is None:
        logger.error("另一 tdx_opt_live 已占锁文件 %s", lockPath)
        releaseTdxOptSingleton()
        return False
    _instanceLockFile = lockFile
    atexit.register(releaseTdxOptSingleton)
    logger.info(
        "单实例已确认：无其它 tdx_opt_live；锁 %s pid=%s",
        lockPath,
        os.getpid(),
    )
    return True


def asFloat(val):
    if val is None or val == "":
        return None
    if isinstance(val, (list, tuple)):
        return asFloat(val[0]) if val else None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if num != num:
        return None
    return num


def toQmtCode(tsCode: str) -> str:
    text = str(tsCode or "").strip().upper()
    if text.endswith(".SH"):
        return text[:-3] + ".SHO"
    if text.endswith(".SZ"):
        return text[:-3] + ".SZO"
    return text


def loadListed(chains: tuple[tuple[str, str], ...] | None = None) -> list[tuple[str, str]]:
    """[(qmtCode, undlSix), ...]；默认拉主链+（若开）兜底链。"""
    use = chains if chains is not None else (
        PRIMARY_CHAINS + (FALLBACK_CHAINS if FALLBACK_ENABLE else ())
    )
    conn = pymysql.connect(**dbConfig())
    try:
        cur = conn.cursor()
        out = []
        for undl, optCode in use:
            cur.execute(
                """
                SELECT ts_code
                FROM opt_basic
                WHERE opt_code=%s
                  AND list_date<=CURDATE()
                  AND (delist_date IS NULL OR delist_date>=CURDATE())
                ORDER BY ts_code
                """,
                (optCode,),
            )
            for row in cur.fetchall():
                out.append((toQmtCode(row[0]), undl))
        return out
    finally:
        conn.close()


def mapTdxRecToPacked(
    rec: dict, undl: str, now: datetime | None = None, prev: dict | None = None
) -> dict | None:
    """通达信报表 → 大 QMT slim tick。

    无交易所时钟：仅在行情内容变化时盖墙钟；内容未变则沿用 prev.time，
    避免段头「实时(TDX) 时分秒」空转而最新/量/仓冻住。
    """
    global _lastRawFieldNames
    if not isinstance(rec, dict):
        return None
    if rec.get("ErrorId") not in ("0", 0, None):
        return None
    if _lastRawFieldNames is None:
        try:
            _lastRawFieldNames = sorted(str(k) for k in rec.keys())
        except Exception:
            _lastRawFieldNames = []
    buyp = rec.get("Buyp") if isinstance(rec.get("Buyp"), list) else []
    sellp = rec.get("Sellp") if isinstance(rec.get("Sellp"), list) else []
    buyv = rec.get("Buyv") if isinstance(rec.get("Buyv"), list) else []
    sellv = rec.get("Sellv") if isinstance(rec.get("Sellv"), list) else []
    last = asFloat(rec.get("Now"))
    if last is not None and last <= 0:
        last = None
    bid1 = asFloat(buyp[0] if buyp else None)
    ask1 = asFloat(sellp[0] if sellp else None)
    vol = asFloat(rec.get("Volume") if rec.get("Volume") is not None else rec.get("Vol"))
    # 持仓：TQ 期货/期权常用 VolInStock（tdxquant 亦映射为 open_interest）
    oi = None
    for key in (
        "VolInStock",
        "OpenInterest",
        "openInterest",
        "openInt",
        "OpenInt",
        "Position",
        "Hold",
    ):
        oi = asFloat(rec.get(key))
        if oi is not None and oi >= 0:
            break
        oi = None
    # 完整五档都属于盘口内容。只比较买一卖一会漏掉二至五档及档位量变化，
    # 使下游误以为盘口未变、仅推进观测时钟而保留旧深档。
    quoteKey = (
        last,
        bid1,
        ask1,
        vol,
        oi,
        tuple(asFloat(x) for x in buyp[:5]),
        tuple(asFloat(x) for x in sellp[:5]),
        tuple(asFloat(x) for x in buyv[:5]),
        tuple(asFloat(x) for x in sellv[:5]),
    )
    prevKey = None
    if isinstance(prev, dict):
        prevKey = (
            asFloat(prev.get("last")),
            asFloat(prev.get("bid1")),
            asFloat(prev.get("ask1")),
            asFloat(prev.get("vol") if prev.get("vol") is not None else prev.get("volume")),
            asFloat(prev.get("oi") if prev.get("oi") is not None else prev.get("openInt")),
            tuple(asFloat(x) for x in (prev.get("bidPrice") or [])[:5]),
            tuple(asFloat(x) for x in (prev.get("askPrice") or [])[:5]),
            tuple(asFloat(x) for x in (prev.get("bidVol") or [])[:5]),
            tuple(asFloat(x) for x in (prev.get("askVol") or [])[:5]),
        )
    if prevKey is not None and quoteKey == prevKey and prev.get("time") is not None:
        ms = float(prev.get("time"))
    else:
        wall = now or datetime.now()
        ms = wall.timestamp() * 1000.0
    packed = {
        "last": last,
        "lastPrice": last,
        "lastClose": asFloat(rec.get("LastClose")),
        "bid1": bid1,
        "ask1": ask1,
        "bidPrice": [asFloat(x) for x in buyp[:5]],
        "askPrice": [asFloat(x) for x in sellp[:5]],
        "bidVol": [asFloat(x) for x in buyv[:5]],
        "askVol": [asFloat(x) for x in sellv[:5]],
        "vol": vol,
        "volume": vol,
        "t": ms,
        "time": ms,
        "fromTdx": True,
        "undl": undl,
    }
    if oi is not None:
        packed["oi"] = oi
        packed["openInt"] = oi
    return packed



def forceRefreshCache(tq, logger, reason: str) -> bool:
    """执行一次 QQ force refresh（会弹通达信框）。"""
    global _lastRefreshMono, _healthBadStreak, _coverageLowStreak
    try:
        tq.refresh_cache(market="QQ", force=True)
        _lastRefreshMono = time.monotonic()
        _healthBadStreak = 0
        _coverageLowStreak = 0
        logger.info("refresh_cache ok reason=%s", reason)
        return True
    except Exception as exc:
        logger.warning("refresh_cache 失败 reason=%s %s", reason, exc)
        return False


def noteOptHealth(listedN: int, fiveN: int, errN: int,
                  staleP75: float = -1.0) -> None:
    """根据本轮五档/失败/陈旧度更新峰值与不健康连续计数。"""
    global _peakFive, _healthBadStreak, _coverageLowStreak, _staleLowStreak
    if listedN <= 0:
        return
    # 仅抬升「已达武装门槛」的峰值，避免集合竞价 thin five 把 peak 锁死在个位数
    armFloor = listedN * FIVE_PEAK_ARM_RATIO
    if fiveN >= armFloor and fiveN > _peakFive:
        _peakFive = fiveN
    armed = _peakFive >= armFloor
    fiveDrop = armed and fiveN < _peakFive * FIVE_DROP_RATIO
    errBad = errN >= listedN * SNAP_ERR_RATIO
    if fiveDrop or errBad:
        _healthBadStreak += 1
    else:
        _healthBadStreak = 0
    coverFloor = max(5, int(listedN * COVERAGE_MIN_RATIO))
    if fiveN < coverFloor:
        _coverageLowStreak += 1
    else:
        _coverageLowStreak = 0
    # 陈旧度判据：这是"关掉周期强刷"的保险丝。此前健康判据只看五档是否缺失，
    # 若本地缓存不再自更新，快照会"字段齐全但时间戳整体变旧"——那样五档判据
    # 永远为假、系统不会自愈。这里按 P75 滞后判，命中才补一次真刷（会弹框，
    # 但只在真的陈旧时出现，正常盘中不再每 3 秒弹）。
    if staleP75 < 0:
        _staleLowStreak = 0
    elif staleP75 >= REFRESH_STALE_P75_SEC:
        _staleLowStreak += 1
    else:
        _staleLowStreak = 0


def inOptSession(now: datetime | None = None) -> bool:
    """是否处于需要保新鲜的期权交易时段。

    用 15:00 作为收盘边界（不是 15:05）：期权连续竞价 15:00 结束，
    之后缓存不再脉冲，陈旧度必然单调上涨 —— 若仍判定为盘内，
    保险丝会在收盘后反复触发强刷（弹框），正是要避免的问题。
    """
    hm = (now or datetime.now()).strftime("%H:%M")
    return ("09:25" <= hm < "11:30") or ("13:00" <= hm < "15:00")


def sessionForceRefreshKey(now: datetime | None = None) -> str:
    """交易时段内**滚动**的强刷键：每 REFRESH_SESSION_SEC 变一次。

    2026-09-11 修：原实现只在开盘/午盘边界各刷一次，实测 TDX 本地行情缓存
    约 10 分钟才自更新一次，导致 09:45 之后整链（含 510500 的 A 类合约）
    长时间停在旧值——`snap n=648 five=647 err=0 refresh=0` 却全是陈旧报价。

    现在盘内按槽位滚动，配合 `REFRESH_COOLDOWN_SEC` 形成周期性真刷。
    实测 `refresh_cache(force=True)` 约 0.9s、返回 Refresh Cache Success，
    但**会让通达信弹"数据刷新"进度条** —— 因此 2026-09-11 起默认
    `REFRESH_SESSION_SEC=0` 关闭本函数（见常量处 A/B 实测），
    只在陈旧度/五档健康恶化时才补刷。
    """
    wall = now or datetime.now()
    hm = wall.strftime("%H:%M")
    day = wall.strftime("%Y%m%d")
    sec = int(REFRESH_SESSION_SEC)
    # 0 = 关闭周期强刷：必须直接返回空键。
    # 早期实现写 `max(1, sec)`，sec=0 时会退化成"每 1 秒一格"，反而比原来更频繁。
    if sec <= 0:
        return ""
    # 盘内槽位：把一天切成 sec 秒一格的序号
    slot = int(wall.timestamp()) // sec
    if "09:25" <= hm < "11:30":
        return f"{day}-am-{slot}"
    if "13:00" <= hm < "15:05":
        return f"{day}-pm-{slot}"
    return ""


def decideForceRefresh(listedN: int) -> tuple[bool, str]:
    """是否强刷：可选启动暖缓存；盘内周期（session 槽位）；可选保底；或健康恶化。"""
    global _lastRefreshMono, _refreshRequested, _refreshRequestReason
    global _lastSessionRefreshKey, _lastSessionRefreshTryMono
    nowMono = time.monotonic()
    if _refreshRequested:
        reason = _refreshRequestReason or "http_refresh"
        _refreshRequested = False
        _refreshRequestReason = ""
        if _lastRefreshMono > 0 and (nowMono - _lastRefreshMono) < REFRESH_COOLDOWN_SEC:
            return False, ""
        return True, reason
    sessKey = sessionForceRefreshKey()
    if sessKey and sessKey != _lastSessionRefreshKey:
        if _lastRefreshMono <= 0 or (nowMono - _lastRefreshMono) >= REFRESH_COOLDOWN_SEC:
            _lastSessionRefreshKey = sessKey
            _lastSessionRefreshTryMono = nowMono
            return True, f"session:{sessKey}"
        # 冷却中：**不要**提前记账，否则该槽位永久错过
        # （槽位每 REFRESH_SESSION_SEC 才滚一次，错过就等下一格）。
        # 这里只按冷却重试，保证槽位滚动后能补上。
        if nowMono - _lastSessionRefreshTryMono >= REFRESH_COOLDOWN_SEC:
            _lastSessionRefreshTryMono = nowMono
    if _lastRefreshMono <= 0:
        if REFRESH_ON_STARTUP:
            return True, "startup"
        # 跳过启动强刷时打点，避免每轮都落进「从未刷过」分支
        _lastRefreshMono = nowMono
        return False, ""
    since = nowMono - _lastRefreshMono
    if since < REFRESH_COOLDOWN_SEC:
        return False, ""
    # 保底：仅当 REFRESH_SAFETY_SEC>0；同样限盘内
    if REFRESH_SAFETY_SEC > 0 and since >= REFRESH_SAFETY_SEC and inOptSession():
        return True, "safety"
    # ⚠️ 所有 fail-safe 补刷都**必须**盘内门控。
    # 事故（2026-09-12/13 实测）：`health_bad` 这一支漏了门控，而盘后快照
    # `five=0`（五档全空）会让健康判据恒判"恶化" →
    #   09-12 15:00~23:00 触发 6734 次、09-13 08:00 又 8 次，
    #   即盘后每 2~4 秒弹一次通达信"数据刷新"框，持续 8 小时。
    # 盘后/盘前根本没有行情可保新鲜，补刷无意义、只有副作用。
    if (listedN > 0 and _healthBadStreak >= HEALTH_BAD_STREAK
            and inOptSession()):
        return True, f"health_bad streak={_healthBadStreak} peakFive={_peakFive}"
    if listedN > 0 and _coverageLowStreak >= COVERAGE_LOW_STREAK and inOptSession():
        return True, (
            f"coverage_low streak={_coverageLowStreak} "
            f"five_min={max(5, int(listedN * COVERAGE_MIN_RATIO))}"
        )
    if listedN > 0 and _staleLowStreak >= STALE_LOW_STREAK and inOptSession():
        # 双保险：阈值已远离锯齿峰值，这里再强制一个最小间隔。
        # 教训（2026-09-14）：锯齿周期变慢到 ≈135s、阈值 120s 时，保险丝
        # 每个锯齿周期都触发 → 退化成"每 2 分钟弹一次框"的周期刷新。
        # 注意：这里不写行内注释 —— 自检的机械检查会先剥掉 `#` 之后的内容，
        # 门控标记必须落在**代码**上（本行的条件 + 上一行的 inOptSession）。
        if since >= STALE_REFRESH_MIN_GAP_SEC:
            return True, (
                f"stale_p75 streak={_staleLowStreak} "
                f"threshold={REFRESH_STALE_P75_SEC:.0f}s"
            )
    return False, ""


def requestForceRefresh(reason: str = "http_refresh") -> None:
    """供 HTTP /refresh 请求下一轮强刷。"""
    global _refreshRequested, _refreshRequestReason
    _refreshRequested = True
    _refreshRequestReason = str(reason or "http_refresh")[:64]


def setSessionRefreshSec(sec: float, logger=None) -> float:
    """运行时调整盘内周期强刷间隔（秒）；0 = 关闭周期刷新（不再弹通达信框）。

    为什么需要运行时开关：`force refresh_cache` 会让通达信客户端弹数据刷新
    进度条，盘中每 3 秒一次会严重干扰正常使用。留一个不改代码就能调/关的入口，
    便于先关掉观察数据是否仍然足够新鲜，再决定长期取值。
    """
    global REFRESH_SESSION_SEC
    try:
        val = max(0.0, float(sec))
    except (TypeError, ValueError):
        return REFRESH_SESSION_SEC
    REFRESH_SESSION_SEC = val
    # 关闭时清空槽位记账，重新开启时能立刻生效（不必等下一个槽位滚动）
    if val <= 0:
        globals()["_lastSessionRefreshKey"] = ""
    if logger is not None:
        logger.info("session refresh sec 已设为 %.1f（0=关闭周期强刷）", val)
    return REFRESH_SESSION_SEC


def requestVisProbe(reason: str = "http_probe") -> None:
    """供 HTTP /probe_volinstock 立刻跑一轮 VolInStock 探针。"""
    global _visProbeRequested
    _visProbeRequested = True
    del reason  # 仅占位，日志在 probe 内写


def _visProbeCell(frame, field: str):
    """从 get_market_data 返回的 dict/DataFrame 取末值；无列则 None。"""
    if not isinstance(frame, dict):
        return None
    raw = frame.get(field)
    if raw is None:
        return None
    try:
        import numpy as np

        arr = np.asarray(raw, dtype=float).ravel()
        vals = [float(v) for v in arr if v == v]
        return vals[-1] if vals else None
    except Exception:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


def pickHotOptCodes(bag: dict, limit: int = 3) -> list[str]:
    """按成交量挑高活跃合约；优先 SHO，不足再补 SZO。"""
    rows: list[tuple[float, str]] = []
    for code, rec in (bag or {}).items():
        if not isinstance(rec, dict):
            continue
        text = str(code)
        if not (text.endswith(".SHO") or text.endswith(".SZO")):
            continue
        vol = asFloat(rec.get("vol") if rec.get("vol") is not None else rec.get("volume"))
        rows.append((float(vol or 0.0), text))
    rows.sort(reverse=True)
    sho = [c for _v, c in rows if c.endswith(".SHO")]
    szo = [c for _v, c in rows if c.endswith(".SZO")]
    out = sho[: max(0, limit)]
    if len(out) < limit:
        out.extend(szo[: limit - len(out)])
    return out


def probeVolInStock(tq, bag: dict, logger) -> dict:
    """小样本测 TQ get_market_data 是否返回 VolInStock（K 线持仓）。

    官方文档写在 K 线上；实测期权常无该列。测不通则不要从 snapshot 硬挖。
    """
    global _lastVisProbeResult, _lastVisProbeMono, _visProbeRequested
    _visProbeRequested = False
    codes = pickHotOptCodes(bag, VIS_PROBE_N)
    result: dict = {
        "ok": True,
        "wall": datetime.now().isoformat(timespec="seconds"),
        "codes": codes,
        "rows": [],
        "hasAnyVis": False,
        "note": "",
    }
    if not codes:
        result["ok"] = False
        result["note"] = "no_hot_codes"
        _lastVisProbeResult = result
        _lastVisProbeMono = time.monotonic()
        logger.warning("VIS_PROBE skip no_hot_codes")
        return result

    for code in codes:
        row = {"code": code, "periods": {}}
        for period in ("1m", "1d"):
            cell: dict = {"ok": False}
            try:
                t0 = time.perf_counter()
                frame = tq.get_market_data(
                    field_list=["Close", "Volume", "VolInStock", "Amount"],
                    stock_list=[code],
                    period=period,
                    count=1,
                    dividend_type="none",
                    fill_data=True,
                )
                keys = sorted(str(k) for k in frame.keys()) if isinstance(frame, dict) else []
                vis = _visProbeCell(frame, "VolInStock")
                cell = {
                    "ok": True,
                    "elapsed_ms": int(round((time.perf_counter() - t0) * 1000)),
                    "keys": keys,
                    "close": _visProbeCell(frame, "Close"),
                    "volume": _visProbeCell(frame, "Volume"),
                    "volInStock": vis,
                    "hasVolInStockCol": "VolInStock" in keys,
                }
                if vis is not None and float(vis) > 0:
                    result["hasAnyVis"] = True
            except Exception as exc:
                cell = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
            row["periods"][period] = cell
        result["rows"].append(row)
        logger.info(
            "VIS_PROBE %s 1m_vis=%s 1d_vis=%s 1d_keys=%s 1m_keys=%s",
            code,
            (row["periods"].get("1m") or {}).get("volInStock"),
            (row["periods"].get("1d") or {}).get("volInStock"),
            ",".join((row["periods"].get("1d") or {}).get("keys") or [])[:80],
            ",".join((row["periods"].get("1m") or {}).get("keys") or [])[:80],
        )

    if result["hasAnyVis"]:
        result["note"] = "tq_volinstock_usable"
        logger.info("VIS_PROBE verdict=usable n=%d", len(codes))
    else:
        result["note"] = "tq_volinstock_absent_do_not_scrape_snapshot"
        logger.info(
            "VIS_PROBE verdict=absent n=%d（勿从 snapshot 挖仓；持仓继续靠 QMT openInt）",
            len(codes),
        )
    _lastVisProbeResult = result
    _lastVisProbeMono = time.monotonic()
    return result


def maybeProbeVolInStock(tq, bag: dict, logger) -> None:
    """按间隔或 HTTP 请求跑 VolInStock 探针。"""
    global _visProbeRequested
    if VIS_PROBE_SEC <= 0 and not _visProbeRequested:
        return
    nowMono = time.monotonic()
    due = _visProbeRequested or (
        VIS_PROBE_SEC > 0
        and (_lastVisProbeMono <= 0 or (nowMono - _lastVisProbeMono) >= VIS_PROBE_SEC)
    )
    if not due:
        return
    try:
        probeVolInStock(tq, bag, logger)
    except Exception:
        logger.exception("VIS_PROBE 失败")


def snapOptQuotes(
    tq, listed: list[tuple[str, str]], logger, prevOpt: dict | None = None
) -> tuple[dict, float, int, float]:
    """只读期权快照，不强制 refresh。返回 bag, elapsed, errN, staleP75Sec。

    `staleP75Sec` = 报价时间戳滞后的 75 分位。为什么用分位而不是最大/均值：
    尾盘有大量不成交合约，滞后会单调增长到数分钟（实测 max 达 194s），
    属正常现象；**正在成交**的合约是否变旧才是有意义的判据。
    """
    t0 = time.perf_counter()
    bag = {}
    now = datetime.now()
    errN = 0
    prevOpt = prevOpt or {}
    for code, undl in listed:
        if not running:
            break
        try:
            rec = tq.get_market_snapshot(code)
        except Exception as exc:
            errN += 1
            if errN <= 3:
                logger.warning("快照异常 %s %s", code, exc)
            continue
        packed = mapTdxRecToPacked(rec, undl, now, prev=prevOpt.get(code))
        if packed:
            bag[code] = packed
        else:
            errN += 1
    return bag, time.perf_counter() - t0, errN, staleP75Sec(bag, now)


def staleP75Sec(bag: dict, now: datetime | None = None) -> float:
    """快照报价时间戳滞后的 75 分位（秒）；无有效时间戳返回 -1。"""
    wall = now or datetime.now()
    refMs = wall.timestamp() * 1000.0
    lags = []
    for rec in bag.values():
        if not isinstance(rec, dict):
            continue
        ms = 0.0
        for key in ("lastTime", "time", "t"):
            raw = rec.get(key)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if val > 1e12:
                ms = val
                break
            if val > 1e9:
                ms = val * 1000.0
                break
        if ms > 0:
            lags.append((refMs - ms) / 1000.0)
    if not lags:
        return -1.0
    lags.sort()
    return lags[int(len(lags) * 0.75)]


def refreshAndSnap(tq, listed: list[tuple[str, str]], logger) -> tuple[dict, float, bool, int, int, float]:
    """按需/保底强刷后再快照。

    返回 bag, elapsed, didRefresh, errN, fiveN, staleP75Sec。
    注意：陈旧度由 `decideForceRefresh` 在**本轮取数之前**判定，所以这里
    返回的是"补刷之后"的陈旧度，用于观察趋势而非触发本轮刷新。
    """
    global _lastOptBag
    listedN = len(listed)
    need, reason = decideForceRefresh(listedN)
    didRefresh = False
    tRefresh = 0.0
    if need:
        t0 = time.perf_counter()
        didRefresh = forceRefreshCache(tq, logger, reason)
        tRefresh = time.perf_counter() - t0
    bag, snapSec, errN, staleP75 = snapOptQuotes(tq, listed, logger, prevOpt=_lastOptBag)
    _lastOptBag = dict(bag)
    fiveN = sum(
        1 for rec in bag.values()
        if (rec.get("bid1") or 0) or (rec.get("ask1") or 0)
    )
    # 注意：**不在这里**做 noteOptHealth。此处只有主链（约 226 只，实际五档
    # 10~30 只），而覆盖率门槛按 listedN*0.35 算 = 79 → 每轮都判 coverage_low、
    # 每 6s 触发一次强刷（弹框）。健康判据统一由 main() 用**合并后**的口径评估；
    # 两处都调还会把 streak 计数双重递增，使阈值提前命中。
    return bag, tRefresh + snapSec, didRefresh, errN, fiveN, staleP75


def mapTdxRecToIdxRow(rec: dict, tsCode: str, name: str, now: datetime) -> dict | None:
    """通达信指数快照 → rt_idx_k 一行。

    盘前常见 Open/Max/Min=0：只要现价与昨收有效仍写入（Pct 可用）；
    open 无效则置 NULL；high/low 无效则回退为 close（振幅暂为 0）。
    """
    if not isinstance(rec, dict):
        return None
    if rec.get("ErrorId") not in ("0", 0, None):
        return None
    close = asFloat(rec.get("Now"))
    preClose = asFloat(rec.get("LastClose"))
    openPx = asFloat(rec.get("Open"))
    highPx = asFloat(rec.get("Max"))
    lowPx = asFloat(rec.get("Min"))
    if close is None or close <= 0 or preClose is None or preClose <= 0:
        return None
    if openPx is not None and openPx <= 0:
        openPx = None
    if highPx is None or highPx <= 0:
        highPx = close
    if lowPx is None or lowPx <= 0:
        lowPx = close
    vol = asFloat(rec.get("Volume") if rec.get("Volume") is not None else rec.get("Vol"))
    amount = asFloat(rec.get("Amount"))
    return {
        "ts_code": tsCode,
        "name": name,
        "trade_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "close": close,
        "pre_close": preClose,
        "high": highPx,
        "open": openPx,
        "low": lowPx,
        "vol": vol,
        "amount": amount,
    }


def snapIdxQuotes(tq, logger) -> dict[str, dict]:
    """拉中证2000/北证50；返回 ts_code → rt_idx_k 行字段。"""
    now = datetime.now()
    out: dict[str, dict] = {}
    for tsCode, name in IDX_TQ_SPECS:
        if not running:
            break
        try:
            rec = tq.get_market_snapshot(tsCode)
        except Exception as exc:
            logger.warning("指数快照异常 %s %s", tsCode, exc)
            continue
        row = mapTdxRecToIdxRow(rec or {}, tsCode, name, now)
        if row:
            out[tsCode] = row
        else:
            logger.debug("指数快照无效 %s", tsCode)
    return out


def upsertRtIdxK(rows: dict[str, dict], logger) -> int:
    """REPLACE 写入 stock_tushare.rt_idx_k；成功条数。"""
    if not rows:
        return 0
    sql = (
        f"REPLACE INTO `{RT_IDX_K_TABLE}` "
        "(ts_code, name, trade_time, close, pre_close, high, open, low, vol, amount) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    params = [
        (
            r["ts_code"],
            r["name"],
            r["trade_time"],
            r["close"],
            r["pre_close"],
            r["high"],
            r["open"],
            r["low"],
            r["vol"],
            r["amount"],
        )
        for r in rows.values()
    ]
    conn = None
    try:
        conn = pymysql.connect(**dbConfig())
        with conn.cursor() as cur:
            cur.executemany(sql, params)
        conn.commit()
        return len(params)
    except Exception as exc:
        logger.warning("rt_idx_k 写入失败 %s", exc)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return 0
    finally:
        if conn:
            conn.close()


def maybeWriteIdxDb(idxRows: dict[str, dict], logger) -> int:
    """按 IDX_DB_WRITE_SEC 节流写库。"""
    global _lastIdxDbMono
    if not idxRows:
        return 0
    nowMono = time.monotonic()
    if _lastIdxDbMono > 0 and nowMono - _lastIdxDbMono < IDX_DB_WRITE_SEC:
        return 0
    n = upsertRtIdxK(idxRows, logger)
    if n > 0:
        _lastIdxDbMono = time.monotonic()
    return n


def publishSnap(opt: dict, elapsed: float, idx: dict | None = None) -> bytes:
    idx = idx or {}
    oiN = sum(
        1
        for rec in opt.values()
        if isinstance(rec, dict)
        and (
            rec.get("oi") is not None
            or rec.get("openInt") is not None
        )
    )
    payload = {
        "ok": True,
        "opt": opt,
        "optN": len(opt),
        "oiN": oiN,
        "idx": idx,
        "idxN": len(idx),
        "wall": datetime.now().isoformat(timespec="seconds"),
        "pullSec": round(elapsed, 3),
    }
    if _lastRawFieldNames is not None:
        payload["tdxFields"] = _lastRawFieldNames
    body = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    with _snapLock:
        global _snapJson
        _snapJson = body
    return body


class TdxSnapHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:
        return

    def _writeJson(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = (self.path or "/").split("?", 1)[0]
        if path in ("/refresh", "/force_refresh"):
            requestForceRefresh("http_get")
            self._writeJson({"ok": True, "queued": True, "reason": "http_get"})
            return
        if path in ("/set_refresh_sec", "/refresh_sec"):
            raw = ""
            if "?" in (self.path or ""):
                query = (self.path or "").split("?", 1)[1]
                for part in query.split("&"):
                    if part.startswith("sec="):
                        raw = part[4:]
            val = setSessionRefreshSec(raw, logging.getLogger("tdxOptLive"))
            self._writeJson({"ok": True, "refreshSessionSec": val})
            return
        if path in ("/probe_volinstock", "/vis_probe"):
            requestVisProbe("http_get")
            self._writeJson(
                {
                    "ok": True,
                    "queued": True,
                    "reason": "http_get",
                    "last": _lastVisProbeResult,
                }
            )
            return
        if path not in ("/snapshot", "/"):
            self.send_error(404)
            return
        with _snapLock:
            body = _snapJson
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = (self.path or "/").split("?", 1)[0]
        if path not in ("/refresh", "/force_refresh"):
            self.send_error(404)
            return
        requestForceRefresh("http_post")
        self._writeJson({"ok": True, "queued": True, "reason": "http_post"})


def startHttp(logger):
    try:
        server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), TdxSnapHandler)
    except OSError as exc:
        logger.error("HTTP 未启动 %s:%s %s", HTTP_HOST, HTTP_PORT, exc)
        return None
    thread = threading.Thread(target=server.serve_forever, name="tdx-opt-http", daemon=True)
    thread.start()
    logger.info("通达信期权快照 http://%s:%s/snapshot", HTTP_HOST, HTTP_PORT)
    return server


def main() -> int:
    global running, _lastFallbackMono, _fallbackBag
    logger = setupLogger()
    tq = None
    try:
        if not acquireTdxOptSingleton(logger):
            return 3
        primaryListed = loadListed(PRIMARY_CHAINS)
        fallbackListed = loadListed(FALLBACK_CHAINS) if FALLBACK_ENABLE else []
        logger.info(
            "listed primary=%d fallback=%d enable_fb=%d fb_s=%.1f "
            "510050=%d 159915=%d 510300=%d 510500=%d 588000=%d "
            "safety_s=%.0f on_startup=%d cooldown_s=%.0f "
            "five_arm=%.2f five_drop=%.2f bad_streak=%d "
            "cover_min=%.2f cover_streak=%d "
            "idx_db_s=%.1f init_retry_s=%.0f idx=%s tdxw=%s "
            "vis_probe_s=%.0f vis_n=%d",
            len(primaryListed),
            len(fallbackListed),
            int(FALLBACK_ENABLE),
            FALLBACK_SEC,
            sum(1 for _c, u in primaryListed if u == "510050"),
            sum(1 for _c, u in primaryListed if u == "159915"),
            sum(1 for _c, u in fallbackListed if u == "510300"),
            sum(1 for _c, u in fallbackListed if u == "510500"),
            sum(1 for _c, u in fallbackListed if u == "588000"),
            REFRESH_SAFETY_SEC,
            int(REFRESH_ON_STARTUP),
            REFRESH_COOLDOWN_SEC,
            FIVE_PEAK_ARM_RATIO,
            FIVE_DROP_RATIO,
            HEALTH_BAD_STREAK,
            COVERAGE_MIN_RATIO,
            COVERAGE_LOW_STREAK,
            IDX_DB_WRITE_SEC,
            TDX_INIT_RETRY_SEC,
            ",".join(c for c, _n in IDX_TQ_SPECS),
            int(isTdxwRunning()),
            VIS_PROBE_SEC,
            VIS_PROBE_N,
        )
        if not primaryListed:
            logger.error("opt_basic 无 510050/159915 在市合约")
            return 2
        tq = connectTdx(logger)
        if startHttp(logger) is None:
            return 4
        while running:
            try:
                resetHealthForNewSession()
                # 健康/强刷仅看主链，避免 400+ 兜底拖垮五档峰值逻辑。
                # 并行模式（TDX_OPT_PARALLEL_PULL=1）：主链强刷+取数 与 兜底取数
                # 相互独立（不同代码空间、纯读），并行可把单轮从 ~4.5s 压到 ~2.8s，
                # 直接降低整链滞后上界。强刷顺序固定为“先刷再取”，与串行一致。
                # 本机 64C/128T（Threadripper 7980X）余量充足；默认开启。
                bag = {}
                elapsed = 0.0
                didRefresh = False
                errN = 0
                fbElapsed = 0.0
                fbErr = 0
                fbag = {}
                fiveN = 0
                nowMono = time.monotonic()
                doFallback = bool(fallbackListed) and (
                    _lastFallbackMono <= 0
                    or (nowMono - _lastFallbackMono) >= FALLBACK_SEC
                )
                if PARALLEL_PULL and doFallback:
                    from concurrent.futures import ThreadPoolExecutor

                    with ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="tdx-opt-pull"
                    ) as pool:
                        futMain = pool.submit(
                            refreshAndSnap, tq, primaryListed, logger
                        )
                        futFb = pool.submit(
                            snapOptQuotes, tq, fallbackListed, logger, _fallbackBag
                        )
                        try:
                            bag, elapsed, didRefresh, errN, fiveN, staleP75 = (
                                futMain.result()
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("并行主链取数失败: %s", exc)
                        try:
                            fbag, fbElapsed, fbErr, _fbStale = futFb.result()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("并行兜底取数失败: %s", exc)
                            fbag = {}
                    if fbag:
                        _fallbackBag = fbag
                        _lastFallbackMono = time.monotonic()
                        _lastOptBag.update(fbag)
                else:
                    bag, elapsed, didRefresh, errN, fiveN, staleP75 = refreshAndSnap(
                        tq, primaryListed, logger
                    )
                    nowMono = time.monotonic()
                    if doFallback:
                        fbag, fbElapsed, fbErr, _fbStale = snapOptQuotes(
                            tq, fallbackListed, logger, prevOpt=_fallbackBag
                        )
                        _fallbackBag = fbag
                        _lastFallbackMono = time.monotonic()
                        _lastOptBag.update(fbag)
                if _fallbackBag:
                    # 主链优先；码空间不重叠
                    merged = dict(_fallbackBag)
                    merged.update(bag)
                    bag = merged
                # 健康判据必须用**合并后**的口径复评一次：
                # `refreshAndSnap` 里的 noteOptHealth 只看主链（226 只），
                # 而覆盖率门槛按该 listedN 的 35% 算（=79），主链实际只有约
                # 10~30 只有五档 → coverage_low 每轮都成立、每 6s 触发一次强刷。
                # 这解释了"关掉周期刷后进度条仍在弹"。合并后五档约 640+，门槛合理。
                mergedFiveN = sum(
                    1 for rec in bag.values()
                    if (rec.get("bid1") or 0) or (rec.get("ask1") or 0)
                )
                noteOptHealth(len(bag), mergedFiveN, errN + fbErr,
                              staleP75Sec(bag))
                idxRows = snapIdxQuotes(tq, logger)
                wrote = maybeWriteIdxDb(idxRows, logger)
                publishSnap(bag, elapsed + fbElapsed, idxRows)
                maybeProbeVolInStock(tq, bag, logger)
                fiveN = sum(
                    1 for rec in bag.values()
                    if (rec.get("bid1") or 0) or (rec.get("ask1") or 0)
                )
                fivePrimary = sum(
                    1
                    for code, undl in primaryListed
                    if (bag.get(code) or {}).get("bid1")
                    or (bag.get(code) or {}).get("ask1")
                )
                logger.info(
                    "snap n=%d five=%d fivePri=%d err=%d fbErr=%d "
                    "idx=%d idxDb=%d pull=%.2fs fb=%.2fs refresh=%s "
                    "staleP75=%.1fs peakFive=%d badStreak=%d coverStreak=%d "
                    "staleStreak=%d",
                    len(bag), fiveN, fivePrimary, errN, fbErr,
                    len(idxRows), wrote, elapsed, fbElapsed,
                    int(didRefresh), staleP75, _peakFive, _healthBadStreak,
                    _coverageLowStreak, _staleLowStreak,
                )
                interruptibleSleep(0.3)
            except Exception:
                logger.exception("一轮 snap 失败，5s 后继续")
                interruptibleSleep(5.0)
    except KeyboardInterrupt:
        running = False
        logger.info("收到中断")
    except Exception:
        logger.exception("启动或运行失败")
        return 1
    finally:
        if tq is not None:
            try:
                tq.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 代码更新时间：2026-09-07 10:52
# 代码更新时间：2026-09-07 18:08 单实例互斥体+LOCALAPPDATA 锁；bat 默认可杀旧进程
# 代码更新时间：2026-09-08 15:06 修复 interruptibleSleep 竞态导致 sleep 负数退出
# 代码更新时间：2026-09-09 06:54 单轮 snap 异常退避续跑，避免整进程退出
# 代码更新时间：2026-09-09 07:28 TQ 写 932000.CSI/899050.BJ → rt_idx_k；HTTP snapshot 附 idx
# 代码更新时间：2026-09-09 09:18 TQ 初始化重试；盘前 Open=0 仍写指数（Pct）
# 代码更新时间：2026-09-09 09:25 单实例加强（双轮杀+互斥持有）；TQ 仅用 .pid 路径去 WARNING
# 代码更新时间：2026-09-09 09:38 默认 TDX_OPT_REFRESH_SEC=90，观察期权质量与弹窗叠窗
# 代码更新时间：2026-09-09 09:51 默认 TDX_OPT_REFRESH_SEC=300
# 代码更新时间：2026-09-09 10:06 强刷改健康按需+保底3600s；冷却120s
# 代码更新时间：2026-09-09 11:55 关闭默认保底强刷（REFRESH_SAFETY_SEC=0）；启动暖缓存可关
# 代码更新时间：2026-09-09 16:49 三链慢刷兜底 510300/510500/588000（FALLBACK_SEC=2.5，可关）
# 代码更新时间：2026-09-10 10:51 映射 VolInStock→oi；三链兜底默认 1.0s；快照附 tdxFields/oiN
# 代码更新时间：2026-09-10 09:27 覆盖率低强刷+peak 不锁薄盘；HTTP /refresh
# 代码更新时间：2026-09-10 11:33 行情未变不重盖 time，避免段头时钟空转
# 代码更新时间：2026-09-10 13:07 上午/午盘开盘各强刷一次TQ缓存
# 代码更新时间：2026-09-10 13:54 VIS_PROBE：盘中 get_market_data(VolInStock)；HTTP /probe_volinstock

# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 10:11 DB_PASSWORD 去掉默认口令
