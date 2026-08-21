# coding: utf-8
"""大 QMT 全推长稳保活：订 7214 增量，窗口内假活则真退真订。不写库、不打底。"""

from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, time as dtTime
from logging.handlers import TimedRotatingFileHandler

SILENCE_SECONDS = 15.0
WATCHDOG_SLEEP_SEC = 1.0
HEARTBEAT_SEC = 60.0
RESUB_FAIL_MAX = 3
GIVE_UP_LOG_SEC = 60.0
SAMPLE_CODES = ("588000.SH", "510300.SH", "000001.SZ")
XTQUANT_SRC = r"D:\cursor\xtquant_big_convert\src"

running = True
_stopLogged = False
_cacheLock = threading.Lock()
stats = {
    "batches": 0,
    "tickUpdates": 0,
    "uniqueCodes": set(),
    "lastPushMono": 0.0,
    "lastPushWall": None,
    "maxGapMs": 0.0,
    "resubOk": 0,
    "resubFail": 0,
    "sampleFiveBand": 0,
    "sampleTicks": 0,
}


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 轮转文件被占用时重试并降级，避免 PermissionError。"""

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        currentTime = int(time.time())
        t = self.rolloverAt - self.interval
        timeTuple = time.gmtime(t) if self.utc else time.localtime(t)
        dfn = self.rotation_filename(
            self.baseFilename + "." + time.strftime(self.suffix, timeTuple)
        )
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


def isWatchdogWindow(now: datetime) -> bool:
    """假活检测窗口：[09:15, 11:30)、[13:00, 15:00)，本地时区。"""
    t = now.time()
    morning = dtTime(9, 15) <= t < dtTime(11, 30)
    afternoon = dtTime(13, 0) <= t < dtTime(15, 0)
    return morning or afternoon


def decideWatchdogAction(
    isWindow: bool,
    silenceSec: float,
    consecutiveFail: int,
    silenceThreshold: float = SILENCE_SECONDS,
    failMax: int = RESUB_FAIL_MAX,
) -> str:
    """idle / check_ping / give_up。check_ping 后需 ping 成功且退订成功才再订。"""
    if consecutiveFail >= failMax:
        return "give_up"
    if not isWindow:
        return "idle"
    if silenceSec < silenceThreshold:
        return "idle"
    return "check_ping"


def canSubscribeAfterUnsubscribe(pingOk: bool, unsubOk: bool) -> bool:
    return bool(pingOk) and bool(unsubOk)


def tickHasFiveBand(tick) -> bool:
    if not isinstance(tick, dict):
        return False
    for key in ("askPrice", "bidPrice", "askPrice1", "bidPrice1"):
        val = tick.get(key)
        if val not in (None, "", 0, 0.0, [], ()):
            return True
    return False


def _sleepInterruptible(totalSec: float) -> None:
    endTs = time.time() + max(0.0, float(totalSec))
    while running and time.time() < endTs:
        time.sleep(min(0.5, endTs - time.time()))


def _ensureXtquantSrc() -> None:
    src = XTQUANT_SRC
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)


def _setupLogging(logger: logging.Logger) -> None:
    logger.setLevel(logging.INFO)
    logFormat = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormat)
    logger.addHandler(consoleHandler)
    logPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "大QMT实时行情.log")
    fileHandler = WindowsSafeTimedRotatingFileHandler(
        logPath, when="midnight", interval=1, backupCount=3, encoding="utf-8"
    )
    fileHandler.setFormatter(logFormat)
    fileHandler.suffix = "%Y%m%d"
    logger.addHandler(fileHandler)


def _onQuote(data) -> None:
    payload = data or {}
    n = len(payload)
    if not n:
        return
    nowMono = time.monotonic()
    with _cacheLock:
        prev = stats["lastPushMono"]
        if prev:
            gapMs = (nowMono - prev) * 1000.0
            if gapMs > stats["maxGapMs"]:
                stats["maxGapMs"] = gapMs
        stats["lastPushMono"] = nowMono
        stats["lastPushWall"] = datetime.now()
        stats["batches"] += 1
        stats["tickUpdates"] += n
        stats["uniqueCodes"].update(payload.keys())
        for code in SAMPLE_CODES:
            tick = payload.get(code)
            if tick is None:
                continue
            stats["sampleTicks"] += 1
            if tickHasFiveBand(tick):
                stats["sampleFiveBand"] += 1


def _rpcPingMs(xtTrader) -> float:
    t0 = time.perf_counter()
    xtTrader.client.call("ping", {})
    return round((time.perf_counter() - t0) * 1000.0, 1)


def _tryForceResub(logger, xtdata, codes, subIdBox) -> bool:
    oldId = subIdBox.get("v")
    if oldId is not None:
        try:
            xtdata.unsubscribe_quote(oldId)
        except Exception as exc:
            logger.error("RESUB_FAIL 退订异常: %s", exc)
            return False
        subIdBox["v"] = None
    try:
        newId = xtdata.subscribe_whole_quote(codes, callback=_onQuote, prime=False)
    except Exception as exc:
        logger.error("RESUB_FAIL 再订异常: %s", exc)
        return False
    subIdBox["v"] = newId
    with _cacheLock:
        stats["lastPushMono"] = time.monotonic()
        stats["lastPushWall"] = datetime.now()
        stats["resubOk"] += 1
    logger.info("RESUB_OK sub_id=%s", newId)
    return True


def _heartbeatLine() -> str:
    with _cacheLock:
        lastWall = stats["lastPushWall"]
        uniqueN = len(stats["uniqueCodes"])
        batches = stats["batches"]
        updates = stats["tickUpdates"]
        maxGap = stats["maxGapMs"]
        resubOk = stats["resubOk"]
        resubFail = stats["resubFail"]
        five = stats["sampleFiveBand"]
        sampleN = stats["sampleTicks"]
    lastTxt = lastWall.strftime("%H:%M:%S") if lastWall else "-"
    fiveTxt = ("%d/%d" % (five, sampleN)) if sampleN else "-"
    return (
        "heartbeat batches=%d updates=%d unique=%d max_gap_ms=%.1f "
        "resub_ok=%d resub_fail=%d last_push=%s five_band=%s"
        % (batches, updates, uniqueN, maxGap, resubOk, resubFail, lastTxt, fiveTxt)
    )


def main() -> int:
    global running, _stopLogged
    logger = logging.getLogger("daqmt_rt_quote")
    _setupLogging(logger)

    def handleStop(signum, _frame):
        global running, _stopLogged
        running = False
        if not _stopLogged:
            logger.info("收到信号 %s，准备退订退出", signum)
            _stopLogged = True

    signal.signal(signal.SIGINT, handleStop)
    signal.signal(signal.SIGTERM, handleStop)

    _ensureXtquantSrc()
    from bigqmt_signal_trader.quote_universe import build_quote_universe, reject_market_codes
    from bigqmt_signal_trader.xtquant_compat import configure, xt_trader, xtdata

    logger.info("configure() … 不要并行 quote-live 订同一 7214 combo")
    configure()
    uni = build_quote_universe(xtdata)
    codes = uni["codes"]
    reject_market_codes(codes)
    if not codes:
        logger.error("全推宇宙为空")
        return 1
    logger.info(
        "宇宙 hs_a=%s bj=%s etf=%s total=%s sectors=%s",
        uni["counts"].get("hs_a"),
        uni["counts"].get("bj"),
        uni["counts"].get("etf"),
        uni["counts"].get("total"),
        uni["sectors"],
    )

    subIdBox = {"v": None}
    try:
        subIdBox["v"] = xtdata.subscribe_whole_quote(codes, callback=_onQuote, prime=False)
    except Exception as exc:
        logger.error("订阅失败: %s", exc)
        return 1
    with _cacheLock:
        stats["lastPushMono"] = time.monotonic()
        stats["lastPushWall"] = datetime.now()
    logger.info("已订阅 sub_id=%s prime=False", subIdBox["v"])

    consecutiveFail = 0
    lastHeartbeat = time.monotonic()
    lastGiveUpLog = 0.0
    try:
        while running:
            _sleepInterruptible(WATCHDOG_SLEEP_SEC)
            if not running:
                break
            nowMono = time.monotonic()
            if nowMono - lastHeartbeat >= HEARTBEAT_SEC:
                pingTxt = "-"
                try:
                    pingTxt = "%.1f" % _rpcPingMs(xt_trader)
                except Exception as exc:
                    pingTxt = "FAIL:%s" % exc
                logger.info("%s ping_ms=%s", _heartbeatLine(), pingTxt)
                lastHeartbeat = nowMono
            with _cacheLock:
                lastPush = stats["lastPushMono"]
            silenceSec = (nowMono - lastPush) if lastPush else 0.0
            if silenceSec < SILENCE_SECONDS:
                consecutiveFail = 0
            action = decideWatchdogAction(
                isWatchdogWindow(datetime.now()),
                silenceSec,
                consecutiveFail,
            )
            if action == "idle":
                continue
            if action == "give_up":
                if nowMono - lastGiveUpLog >= GIVE_UP_LOG_SEC:
                    logger.error(
                        "RESUB_GIVE_UP consecutive_fail=%d silence_s=%.1f",
                        consecutiveFail,
                        silenceSec,
                    )
                    lastGiveUpLog = nowMono
                continue
            try:
                pingMs = _rpcPingMs(xt_trader)
            except Exception as exc:
                logger.warning("PING_FAIL %s silence_s=%.1f", exc, silenceSec)
                continue
            logger.info("假活检测 ping_ms=%.1f silence_s=%.1f，尝试真退真订", pingMs, silenceSec)
            ok = _tryForceResub(logger, xtdata, codes, subIdBox)
            if ok:
                consecutiveFail = 0
            else:
                consecutiveFail += 1
                with _cacheLock:
                    stats["resubFail"] += 1
    finally:
        oldId = subIdBox.get("v")
        if oldId is not None:
            try:
                xtdata.unsubscribe_quote(oldId)
                logger.info("已退订 sub_id=%s", oldId)
            except Exception as exc:
                logger.warning("退出时退订失败: %s", exc)
        try:
            session = getattr(xtdata, "_quote_session", None)
            if session is not None:
                session.stop()
        except Exception:
            pass
        logger.info("退出 %s", _heartbeatLine())
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 代码更新时间：2026-08-21 19:24
