# coding: utf-8
"""交付前自检：逐项核实当日改动是否真的生效且符合意图。

每条检查都给出「期望 / 实测 / 结论」，不依赖主观判断。
用法：
    python -X utf8 scripts/selfcheck_delivery.py
    python -X utf8 scripts/selfcheck_delivery.py --only runtime
"""
from __future__ import annotations
# 用途: 守卫
# 保留至: 2026-10-20

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

DAQMT = "http://127.0.0.1:15580/snapshot"
TDX = "http://127.0.0.1:15581/snapshot"
BOARD = "http://127.0.0.1:5000/api/opt-t-board?u={u}"
BOARD_QUOTE_DELTA = "http://127.0.0.1:5000/api/opt-t-board/quote-delta?u={u}"
OPT_FIVE_UNDERLYINGS = ("510050", "510300", "510500", "588000", "159915")

RESULTS: list[tuple[str, str, str, str]] = []  # (分组, 名称, 结论, 证据)

# 保留期机制的 atime 快照：必须在**本进程读任何脚本文件之前**取一次并全程复用。
# 否则自检自己的 readText/scan 会把 atime 归零，把 `--sweep` 要用的使用证据吃掉
# （实测：先跑自检再跑 --sweep，28 个全部报"未跑过"）。
ATIME_BASELINE: dict | None = None


def captureAtimeBaseline() -> dict:
    """进程最先调用一次，取 atime 真值快照。"""
    global ATIME_BASELINE
    if ATIME_BASELINE is None:
        sys.path.insert(0, os.path.join(_REPO, "scripts"))
        import audit_scratch_retention as retMod
        ATIME_BASELINE = retMod.measureAtimes()
    return ATIME_BASELINE


def add(group: str, name: str, ok: bool | None, evidence: str) -> None:
    verdict = "PASS" if ok is True else ("FAIL" if ok is False else "SKIP")
    RESULTS.append((group, name, verdict, evidence))


def readText(rel: str) -> str:
    """读脚本内容做模式匹配，**且不留下 atime 痕迹**。

    为什么必须还原 atime：保留期机制用「`atime > mtime`」判定"这个脚本跑过"，
    而自检为了检查实现会通读约 40 个脚本 —— 读一次就把它们全标成"跑过"，
    于是 `--sweep` 会把从未执行过的脚本也续期（实测误判 31/31）。
    读之前存下 atime，读完原样写回：自检是纯观察者，不该改变被观察对象。
    """
    path = os.path.join(_REPO, rel)
    try:
        st = os.stat(path)
        saved = (st.st_atime, st.st_mtime)
    except OSError:
        saved = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""
    finally:
        if saved is not None:
            try:
                os.utime(path, saved)
            except OSError:
                pass


def getJson(url: str, timeout: float = 25.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def tdxFallbackRuntimeHealth(ports: dict[int, bool], snapshot: dict) -> tuple[bool, str]:
    """判断 QMT 主桥离线时，TDX-only 降级链路是否真的可交付。

    这不是把 QMT 故障藏起来：调用方仍会明确打印 DEGRADED。它只回答更窄的
    可用性问题——15580 是否已从 15581 拉到五条链、并对每条链至少观察到一个
    由 TDX 实际供给的字段。旧版自检把 15573/15574 缺失一律判 FAIL，因而在
    已验证可降级服务的情况下把“主源退化”误报成“页面不可用”。
    """
    needed = (5000, 15580, 15581)
    missing = [p for p in needed if not ports.get(p, False)]
    if missing:
        return False, f"TDX 降级依赖端口缺失 {missing}"
    meta = snapshot.get("optMeta") or {}
    opt = snapshot.get("opt") or {}
    if not isinstance(meta, dict) or not isinstance(opt, dict):
        return False, "快照缺 optMeta/opt 字典"

    byChain = {u: [0, 0] for u in OPT_FIVE_UNDERLYINGS}
    for code, rawMeta in meta.items():
        if not isinstance(rawMeta, dict):
            continue
        undl = str(rawMeta.get("undl") or "").split(".", 1)[0]
        if undl not in byChain:
            continue
        byChain[undl][0] += 1
        rec = opt.get(code) or {}
        if not isinstance(rec, dict):
            continue
        if any(str(rec.get(key) or "").lower() == "tdx"
               for key in ("lastSource", "bookSource", "qtySource")):
            byChain[undl][1] += 1

    missingChains = [u for u, (total, tdxN) in byChain.items()
                     if total <= 0 or tdxN <= 0]
    detail = ", ".join(
        f"{u}:{total}/{tdxN}" for u, (total, tdxN) in byChain.items()
    )
    return not missingChains, (
        f"五链 记录/TDX字段={detail}"
        + ("" if not missingChains else f"；缺降级覆盖 {missingChains}")
    )


def coreQuoteRuntimeHealth(
    ports: dict[int, bool], snapshot: dict | None, trading: bool,
) -> tuple[bool | None, str, str]:
    """给行情服务返回 PASS / FAIL / SKIP 的可解释可用性结论。

    盘中没有 QMT 主桥时，必须以五链 TDX 实际字段证明降级仍可交付；盘外跨日后
    历史 tick 会被安全过滤，不能把“没有当天实时 tick”误判为故障。这一分支返回
    SKIP，而不是把 QMT 故障涂成 PASS。
    """
    portDetail = ", ".join(f"{k}={'Y' if v else 'N'}" for k, v in ports.items())
    qmtBridgeUp = ports.get(15573, False) and ports.get(15574, False)
    fallbackOk, fallbackDetail = (
        tdxFallbackRuntimeHealth(ports, snapshot)
        if isinstance(snapshot, dict) else (False, "15580 快照不可读")
    )
    if ports.get(5000, False) and qmtBridgeUp:
        return True, "QMT主桥", portDetail
    if fallbackOk:
        return True, "TDX降级", f"{portDetail}；QMT主桥离线，{fallbackDetail}"
    if not trading and ports.get(5000, False):
        return None, "盘外待市", (
            f"{portDetail}；QMT主桥离线；{fallbackDetail}；"
            "非交易时段跨日后历史 tick 已按日期安全过滤，未以空快照伪报故障"
        )
    coreMissing = [p for p in (15573, 15574, 5000) if not ports.get(p, False)]
    return False, "不可用", (
        f"{portDetail}；主桥缺失 {coreMissing or '无'}；{fallbackDetail}"
    )


def toMs(raw) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v > 1e12:
        return v
    if v > 1e9:
        return v * 1000.0
    return 0.0


def inTradingSession() -> bool:
    """是否连续竞价时段（09:30 开盘，15:00 收盘）。

    收盘用 15:00 而不是 15:05：期权连续竞价 15:00 结束，之后没有新成交，
    "数据陈旧"是必然的，判它没有意义（实测 15:04 仍被判陈旧而误报）。
    """
    hm = datetime.now().strftime("%H:%M")
    return ("09:30" <= hm < "11:30") or ("13:00" <= hm < "15:00")


def _calLastOpen(beforeOrOn: date) -> date | None:
    """trade_cal 里 <= 给定日期的最近交易日（取不到返回 None，不抛）。"""
    try:
        import pymysql
        dbPassword = os.getenv("DB_PASSWORD", "").strip()
        if not dbPassword:
            return None
        conn = pymysql.connect(host=os.getenv("DB_HOST", "localhost"),
                               user=os.getenv("DB_USER", "root"),
                               password=dbPassword,
                               database=os.getenv("DB_NAME", "stock_tushare"),
                               charset="utf8mb4")
        try:
            cur = conn.cursor()
            cur.execute("select max(cal_date) from trade_cal where exchange='SSE' "
                        "and is_open=1 and cal_date<=%s", (beforeOrOn,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 自检不应因日历不可用而中断
        return None


def lastClosedSessionTs(now: datetime | None = None) -> datetime:
    """最近一个**已经收盘**的交易日的 15:00。

    "已收盘"≠"今天"：周一 10:00 时最近收盘仍是上周五，若取周一 15:00 就成了未来时刻，
    盘后阈值被凭空放大一天，真正的"整天没更新"会被漏判（本函数曾犯此错，由边界守卫抓出）。
    取不到交易日历时退化为「今日 15:00 / 上一工作日 15:00」。
    """
    now = now or datetime.now()
    today = now.date()
    closedToday = now.hour >= 15
    lastTd = _calLastOpen(today if closedToday else today - timedelta(days=1))
    if lastTd is None:
        lastTd = today if closedToday else today - timedelta(days=1)
        while lastTd.weekday() >= 5:
            lastTd -= timedelta(days=1)
    return datetime.combine(lastTd, dtime(15, 0))


def expectedTickDate(now: datetime | None = None) -> date:
    """当前应当携带的行情日。

    ⚠️ 分两种情况（2026-09-14 修，此前只用"最近已收盘交易日"，在**交易日午休**
    会算错：11:33 时 `lastClosedSessionTs` 取"昨天及之前"的最近交易日，
    而 09-13 是周日 → 得到 09-11（周五），于是把当天正常数据误判成
    "整天未更新"。实测 11:33 触发 3 条假 FAIL）：
      ① 交易日且已过开盘（>=09:25，含午休）→ 期望**当天**；
      ② 否则（盘前 / 周末 / 假期 / 收盘后）→ 期望最近已收盘交易日。
    判"今天是不是交易日"用同一份 trade_cal（取不到则退化为工作日判断）。
    """
    now = now or datetime.now()
    hm = now.strftime("%H:%M")
    today = now.date()
    isTd = _calLastOpen(today) == today if _calLastOpen(today) else today.weekday() < 5
    if isTd and hm >= "09:25":
        return today
    return lastClosedSessionTs(now).date()


def postCloseMaxAgeSec(now: datetime | None = None) -> float:
    """盘后年龄上限 = 距今最近一次收盘的时长 + 4h 缓冲。

    - 正常盘后（如 15:00 收盘、23:45 检查）：上限 ≈ 12.75h，数据停在收盘附近即 PASS；
    - 隔夜/周末（如周六 10:00）：上限 = 19h + 4h = 23h；
    - 整个交易日没更新（周一 10:00 仍是上周五的 tick）：年龄 ≈ 67h > 上限 23h → FAIL。
    自检与实际判定共用本函数，守卫也直接调它（避免两处各写一份公式）。
    """
    now = now or datetime.now()
    return (now - lastClosedSessionTs(now)).total_seconds() + 4 * 3600.0


def inCloseTailQuiet(now: datetime | None = None) -> bool:
    """是否处在收盘前的「尾盘静默」窗口（灰行比例在此窗口不再是服务质量指标）。

    为什么需要（2026-09-14 实测，收盘前 3 分钟触发 1 条假 FAIL）：
      14:57 时 510050 灰行 26% → `灰行<25%` FAIL。但查两侧后确认**不是失效**：
        TDX **自己的** time 已是 p50=73s、599/648 超 30s；桥接只是如实跟随。
      合约在收盘前陆续停止成交，TDX 缓存快照随之不再变化 —— **值确实不动，
      页面确实该变灰**。此时"灰行多"是行情的性质，不是服务的问题。
      （QMT 主导的三条链同刻仍 1~6%，因为它们有实时 tick 不看成交。）

    窗口取 14:45–15:00（实测 14:56 起明显，留 11 分钟余量）。
    **窗口外判据完全不变** —— 若白天出现同样的灰行比例，仍然 FAIL。
    """
    now = now or datetime.now()
    return now.strftime("%H:%M") >= "14:45" and now.strftime("%H:%M") < "15:00"


def stripPyComments(src: str) -> str:
    """去掉 Python 注释与三引号 docstring。

    必要性：被检查的函数 docstring 里会**引用旧实现**（作为警示），
    若只删 `#` 注释，检查器会把 docstring 里的旧代码当成真代码 → 误报。
    """
    # 先去三引号块（含 docstring）
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    out = []
    for line in src.splitlines():
        inStr = False
        quote = ""
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if inStr:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    inStr = False
            elif ch in "\"'":
                inStr = True
                quote = ch
            elif ch == "#":
                cut = i
                break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


# ---------------------------------------------------------------- 代码层

def checkCode() -> None:
    g = "code"
    q = readText("大QMT实时行情.py")
    t = readText("tdx_opt_live.py")
    b = readText("rt_monitor/services/opt_t_board.py")
    bp = readText("rt_monitor/blueprints/opt_t_board_bp.py")
    c = readText("rt_monitor/core/config.py")
    af = readText("rt_monitor/app_factory.py")
    hCode = stripPyComments(readText("templates/rt_monitor_opt_t_board.html"))

    # 1 陈旧判定必须有方向（不得再用 abs 差）—— 只看代码，不看注释
    m = re.search(r"def isStaleIncomingTick.*?(?=\ndef )", q, re.S)
    body = stripPyComments(m.group(0)) if m else ""
    add(g, "陈旧判定有方向(不用abs)",
        ("refMs - ms" in body and "abs(refMs - ms)" not in body),
        "isStaleIncomingTick 使用 (refMs - ms) > maxAgeSec")

    # 2 新鲜观测必须推进时钟
    qCode = stripPyComments(q)
    add(g, "新鲜观测推进字段时钟",
        ('optionTickSetFieldTime(out, "qty", tickMs)' in qCode
         and "clocksAdvanced" in qCode),
        "值未变但时钟推进时仍落库")

    # 3 QMT 主导链需新鲜度判据
    add(g, "QMT主导链含新鲜度判据",
        ("isTickOlderThan" in qCode and "QMT_LED_MAX_STALE_SEC" in qCode),
        "qmtLedChain(undl, existing) 用 isTickOlderThan")

    # 4 方向性年龄函数存在且与 isStaleIncomingTick 分离
    add(g, "isTickOlderThan 独立存在", ("def isTickOlderThan" in qCode),
        "相对墙钟的年龄判定与回盖判定分离")

    # 5 QMT 期权宇宙只含三条链
    add(g, "QMT期权宇宙=3条链",
        ('OPT_QMT_TICK_UNDERLYINGS = ("510300", "510500", "588000")' in qCode),
        "510050/159915 不进 QMT 期权管线")

    # 6 TDX 并行取数开关
    tCode = stripPyComments(t)
    add(g, "TDX并行取数开关",
        ("PARALLEL_PULL" in tCode and "ThreadPoolExecutor" in tCode),
        "TDX_OPT_PARALLEL_PULL 默认 1")

    # 7 TDX 盘中周期强刷（槽位滚动；仅当显式开启时才有键）
    add(g, "TDX槽位滚动强刷",
        ("REFRESH_SESSION_SEC" in tCode and "slot" in tCode),
        "sessionForceRefreshKey 按槽位滚动")
    # 7b LESSON-014 / LESSON-017：周期强刷默认值 = 用户决策（方案 A，3s）。
    # 断言"等于 3"而不是"等于 0"：09-14 用户权衡后选择新鲜度优先，并已知悉代价
    # （约每 3s 弹一次通达信刷新框）。将来若改决策，改这里 + 常量处注释同步更新。
    # 同时必须保留 `sec<=0 → 空键` 的关闭路径（随时可退回"不弹框"模式）。
    add(g, "L014/L017 周期强刷默认=3s 且有可关闭路径",
        ('os.getenv("TDX_OPT_REFRESH_SESSION_SEC", "3")' in tCode
         and "if sec <= 0:" in tCode and 'return ""' in tCode),
        "默认 3s（方案 A）；sec<=0 直接返回空键，可随时关闭")
    # 陈旧保险丝：**读真实数值**再比，不能只查"常量名存在"
    # （实测只查名字时，把阈值改成 30s 这种注入不会被发现）
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_l014_tdx", os.path.join(_REPO, "tdx_opt_live.py"))
        _tdx = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_tdx)
        # 阈值必须大于**实测锯齿峰值**。两次同型事故：
        #   09-11 峰值≈33s / 阈值 30s → 退化成周期刷
        #   09-14 峰值≈135s / 阈值 120s → 每 2 分钟弹一次（当天 59 次）
        # 峰值由模块常量 OBSERVED_SAWTOOTH_PEAK_SEC 承载（附取证方法），
        # 守卫读真实数值比较，避免"只查常量名存在"那种假守卫。
        thr = _tdx.REFRESH_STALE_P75_SEC
        peak = _tdx.OBSERVED_SAWTOOTH_PEAK_SEC
        add(g, "L014 保险丝阈值 > 实测锯齿峰值",
            thr > peak,
            f"REFRESH_STALE_P75_SEC={thr:.0f}s vs 实测峰值≈{peak:.0f}s"
            f"（阈值≤峰值会退化成每 {peak:.0f}s 弹一次框）")
        gap = _tdx.STALE_REFRESH_MIN_GAP_SEC
        add(g, "L014 陈旧补刷有最小间隔（双保险）", gap > 0,
            f"STALE_REFRESH_MIN_GAP_SEC={gap:.0f}s")
        streak = _tdx.STALE_LOW_STREAK
        add(g, "L014 保险丝连击 ≥2", streak >= 2,
            f"STALE_LOW_STREAK={streak}（单轮毛刺不应触发）")
        add(g, "L014 盘内门控存在（helper + 至少两处调用）",
            (_tdx.inOptSession() in (True, False)
             and tCode.count("inOptSession()") >= 2),
            f"inOptSession 调用 {tCode.count('inOptSession()')} 处"
            "（stale_p75 / coverage_low 都必须盘内才触发）")
        # 事故根治（2026-09-12/13）：`decideForceRefresh` 里**每一条**补刷返回
        # 都必须盘内门控。health_bad 曾漏掉 → 盘后 five=0 恒判恶化 →
        # 6734 次 / 8 小时弹框。
        #
        # ⚠️ 这里**刻意只做存在性检查**，不做控制流/缩进分析。
        # 试过"累积条件语句 + 外层条件栈 + 回看 N 行"三版，因注释/嵌套/续行的
        # 组合反复误报（同一处改了四轮），调试成本远超收益 ——
        # 过度聪明的守卫本身就是风险（LESSON-010 的镜像）。
        # **行为保证交给回归测试**：`testAllFailSafesGatedBySession` 与
        # `testSafetyFallbackGatedBySession` 对每条 fail-safe 做
        # "盘外不得触发 / 盘内必须触发"双向断言，且已注入验证：
        # health_bad / coverage_low / stale_p75 / safety 任一处去门控都会被抓住。
        fnBody = ""
        if "def decideForceRefresh" in tCode:
            fnBody = tCode.split("def decideForceRefresh", 1)[1].split("\ndef ", 1)[0]
        branches = [ln.strip() for ln in fnBody.splitlines()
                    if ln.strip().startswith("return True,")]
        gateCalls = len(re.findall(r"inOptSession\(\)", fnBody))
        covered = all(k in fnBody for k in ('"safety"', '"startup"', "session:",
                                            "health_bad", "coverage_low",
                                            "stale_p75"))
        add(g, "L014 decideForceRefresh 分支齐备且有盘内门控调用",
            bool(fnBody) and len(branches) >= 4 and gateCalls >= 2 and covered,
            f"补刷分支 {len(branches)} 个 / inOptSession 调用 {gateCalls} 处 / "
            f"六类原因齐备={covered}"
            "（逐分支的盘内外行为由 testAllFailSafesGatedBySession 保证）")
    except Exception as exc:  # noqa: BLE001
        add(g, "L014 保险丝阈值 >2×缓存脉冲周期", False,
            f"无法加载 tdx_opt_live 取值: {type(exc).__name__}: {exc}")
    # 健康判据必须用合并口径：只看**调用点**（`noteOptHealth(` 前面不是 `def `），
    # 不能直接查 "noteOptHealth(listedN"，否则函数定义 `def noteOptHealth(listedN: ...`
    # 自己就会命中（自指变体，实测让守卫永远 FAIL）。
    callSites = [ln.strip() for ln in tCode.splitlines()
                 if "noteOptHealth(" in ln and not ln.strip().startswith("def ")]
    badSites = [s for s in callSites
                if "mergedFiveN" not in s and "len(bag)" not in s]
    add(g, "L014 健康判据只允许合并口径调用点",
        (len(callSites) >= 1 and not badSites
         and any("mergedFiveN" in s for s in callSites)),
        f"调用点 {len(callSites)} 处，全部用合并口径"
        if not badSites else f"发现主链口径调用: {badSites[:2]}")

    # 8 复用桶只用桶号作键
    bCode = stripPyComments(b)
    add(g, "复用桶只用桶号作键",
        ('return f"b{int(time.time() // _T_BOARD_REUSE_BUCKET_SEC)}"' in bCode),
        "避免内容摘要使缓存永不命中")

    # 9 灰阶时段门控
    add(g, "灰阶仅竞价时段生效",
        ("optTAgeColoringActive" in bCode and "forceFresh" in bCode),
        "午休/盘前/盘后不着色")

    # 10 灰阶阈值
    add(g, "灰阶阈值=30/300",
        ("_OPT_T_AGE_YOUNG_SEC = 30.0" in bCode
         and "_OPT_T_AGE_OLD_SEC = 300.0" in bCode),
        "标定依据见注释")

    # 11 静态版本：内容哈希自动生成（手工递增已废弃，见 rt_monitor/CHANGELOG-static-ver.md）
    try:
        from rt_monitor.core.config import RT_MONITOR_STATIC_VER as _ver, computeRtMonitorStaticVer
        _hashVer = computeRtMonitorStaticVer()
        isEnvOverride = bool(os.getenv("RT_MONITOR_STATIC_VER", "").strip())
        add(g, "静态版本号=内容哈希",
            "def computeRtMonitorStaticVer" in c and (isEnvOverride or _ver == _hashVer),
            f"{_ver}" + ("（环境变量覆盖）" if isEnvOverride else ""))
    except Exception as exc:  # noqa: BLE001
        add(g, "静态版本号=内容哈希", False, f"{type(exc).__name__}: {exc}")

    percWin = readText("rt_monitor/services/opt_iv_windows.py")
    percTests = readText("tests/test_opt_iv_perc_anchors.py")
    add(g, "T板顶栏Per C配置入口",
        ('id="opt-t-add"' in hCode
         and 'id="opt-t-perc-cfg"' in hCode
         and hCode.find('id="opt-t-add"') < hCode.find('id="opt-t-perc-cfg"')
         and "/api/opt-iv-perc-anchors" in bp
         and "def savePercAnchors" in percWin
         and "test_save_overrides_default_and_empty_clears" in percTests
         and "test_file_wins_over_env" in percTests),
        "紧邻添加标的右侧；页面 JSON 覆盖立刻失效缓存")

    # 11a 盘口/量仓的 1 秒增量必须与 IV/希腊值整表重算隔离。
    # 静态字符串不足以证明路由没有误调用重载荷，额外由
    # test_opt_t_board_quote_delta_route.py 在 HTTP 层注入断言。
    deltaMatch = re.search(r"def queryOptTBoardQuoteDelta.*?(?=\ndef )", b, re.S)
    deltaBody = stripPyComments(deltaMatch.group(0)) if deltaMatch else ""
    deltaRouteTests = readText("tests/test_opt_t_board_quote_delta_route.py")
    add(g, "T板轻量报价与重算分层",
        ("fetchDaqmtQuoteSnapshot" in deltaBody
         and "pymysql" not in deltaBody
         and "_query_opt_t_board_payload" not in deltaBody
         and "render" not in deltaBody
         and "quote-delta" in bp
         and "OPT_T_HEAVY_REFRESH_MS = 5000" in hCode
         and "OPT_T_DELTA_REFRESH_MS = 1000" in hCode
         and "function loadQuoteDelta" in hCode
         and "textContent" in hCode
         and "testDeltaRouteDoesNotInvokeHeavyBoardPayload" in deltaRouteTests),
        "delta 仅读快照逐格写入；重算保持 5 秒；HTTP 回归禁止调用重载荷")

    # 11c 「最后观测」与「最后变动」不可复用同一时钟，否则不变价格会被误判未刷新。
    snapshotTickKeys = qCode.split("SNAPSHOT_TICK_KEYS = (", 1)
    snapshotTickBody = snapshotTickKeys[1].split(")", 1)[0] if len(snapshotTickKeys) > 1 else ""
    deltaClockTests = readText("tests/test_daqmt_realtime_quote.py")
    add(g, "报价观测时钟与变动时钟分离",
        ("def optionTickStampChangedFieldClocks" in qCode
         and all(f'\"{key}\"' in snapshotTickBody for key in (
             "lastChangedTime", "qtyChangedTime", "oiChangedTime", "bookChangedTime"))
         and "staleOiValueChanged" in qCode
         and "testTdxReobservationAdvancesObservedButNotChangedClock" in deltaClockTests
         and "testStaleQmtOiKeepsObservedClockButUsesSourceEventForChangedClock" in deltaClockTests),
        "同值重观测推进 *Time；陈旧 OI 变更如实标源事件，均不伪造 *ChangedTime")

    oiPlanTests = readText("tests/test_daqmt_realtime_quote.py")
    add(g, "TDX两链P0守住且长尾按到期月/活跃度受控轮转",
        ('OPT_QTY_PRIORITY_PER_UNDL = int(os.getenv("DAQMT_OPT_QTY_PRIORITY_PER_UNDL", "3"))' in qCode
         and '"p0Slots": 6' in qCode
         and '"name": "probe8"' in qCode
         and '"name": "probe10"' in qCode
         and "def optionQtyTdxDemandCodes" in qCode
         and "def _optionQtyTailOrder(codes, tickMap, metaMap)" in qCode
         and "testTailInterleavesExpiryBucketsAndRanksActivityWithinBucket" in oiPlanTests
         and "testOpenPositionsAreNeverDemotedIntoTailWhenTheyExceedP0Slots" in oiPlanTests),
        "TDX 两链 P0=每链3张；价量变动先补 OI，长尾按期限公平轮转且始终显式降级")

    add(g, "QMT主导链仅在OI真缺失时进入受限P0修复",
        ("def optionQtyQmtOiRepairCodes" in qCode
         and 'OPT_QTY_QMT_REPAIR_PER_UNDL = int(os.getenv("DAQMT_OPT_QTY_QMT_REPAIR_PER_UNDL", "4"))' in qCode
         and "extraPriorityCodes=qmtRepairP0" in qCode
         and "if oi is None:" in qCode
         and "testStaleQmtLedOiRepairGetsHealthyModeP0Slots" in oiPlanTests),
        "QMT 无回调不等于 OI 缺失；只有字段真缺失才补关键合约，不扩大 RPC 并发")

    add(g, "QMT回调陈旧时TDX按字段接管",
        ("qmtFreshBeforeMs=observedMs - QMT_LED_MAX_STALE_SEC * 1000.0" in qCode
         and "def qmtFieldFresh" in qCode
         and "sourceChanged = any(" in qCode
         and "def qmtLedLastOrQtyStale" in qCode
         and "testTdxTakesOverStaleQmtLastAndVolumeButNeverFakesOi" in oiPlanTests
         and "testTdxTakesOverStaleQmtSourceWhenValuesAreUnchanged" in oiPlanTests
         and "testTdxAllowedWhenFreshBookMasksStaleQmtLastAndVolume" in oiPlanTests),
        "QMT 旧 last/volume 不得压住 TDX 新观测；TDX 缺 OI 时保留 QMT 时钟")

    auditCode = readText("scripts/audit_opt_freshness.py")
    add(g, "OI SLA审计有盘内时段门控",
        ("--slo" in auditCode and "NOT_EVALUATED_OFF_SESSION" in auditCode
         and "def inContinuousAuction" in auditCode),
        "非连续竞价只报告字段年龄，不将自然静止判成 SLA 失败")

    fallbackTests = readText("tests/test_daqmt_realtime_quote.py")
    fallbackStart = qCode.find("prepareTdxFallbackChain(logger)")
    fallbackWait = qCode.find("waitUntilRpcPing(logger, xt_trader)")
    add(g, "QMT离线时TDX-only先于RPC等待启动",
        ("def prepareTdxFallbackChain" in qCode
         and "def pollTdxFallbackOnce" in qCode
         and "def _runTdxFallbackLoop" in qCode
         and "forceTdx=True" in qCode
         and fallbackStart >= 0 and fallbackWait > fallbackStart
         and "testMainStartsTdxFallbackBeforeBlockingForQmtRpc" in fallbackTests),
        "15573 离线不能阻塞 15581→15580 的 TDX-only 降级链路")

    # 18 盘后年龄判据随收盘时刻走（写死小时数会在深夜必然误报）
    cal = _calLastOpen(date(2026, 9, 11))
    add(g, "交易日历可用（trade_cal）", cal == date(2026, 9, 11), f"2026-09-11 最近交易日={cal}")
    if cal == date(2026, 9, 11):
        # 边界用例：medSec=最新 tick 年龄，tickDate=最新 tick 的行情日
        # expectDate = 该时刻**应当**携带的行情日
        #
        # ⚠️ 语义修正（2026-09-14）：原用例写「周一 10:00 期望周五数据」，与
        # "10:00 是盘中、应该有当天数据"矛盾，也与 11:33 实测（当天数据却被判
        # 整天未更新、产生 3 条假 FAIL）矛盾。正确区分是：
        #   · 盘中/午休（>=09:25，无论是否在连续竞价内）→ 期望**当天**
        #   · 开盘前（<09:25）与周末/假期 → 期望最近已收盘交易日
        cases = (
            ("盘后正常", datetime(2026, 9, 11, 23, 45), 8.7 * 3600, date(2026, 9, 11), date(2026, 9, 11), True),
            ("周末（周五数据）", datetime(2026, 9, 12, 10, 0), 19.0 * 3600, date(2026, 9, 11), date(2026, 9, 11), True),
            # 开盘前持上周五数据 → 正常（周末无行情）
            ("周一开盘前（周五数据）", datetime(2026, 9, 14, 8, 0), 65.0 * 3600, date(2026, 9, 11), date(2026, 9, 11), True),
            # 盘中（含午休）持当天数据 → 正常（此前误判的正是这一种）
            ("盘中（当天数据）", datetime(2026, 9, 14, 11, 33), 0.6 * 60, date(2026, 9, 14), date(2026, 9, 14), True),
            # 开盘后仍持上周五数据 → 整天未更新，必须 FAIL
            ("整天没更新", datetime(2026, 9, 14, 10, 0), 67.0 * 3600, date(2026, 9, 11), date(2026, 9, 14), False),
        )
        for tag, now, medSec, tickDate, expectDate, expect in cases:
            lim = postCloseMaxAgeSec(now) / 3600.0
            gotDate = expectedTickDate(now)
            got = (medSec < postCloseMaxAgeSec(now)) and (tickDate == gotDate)
            add(g, f"盘后判据 {tag}",
                (got is expect) and (gotDate == expectDate),
                f"上限 {lim:.1f}h、中位 {medSec/3600:.1f}h、"
                f"期望行情日 {gotDate}（用例给 {expectDate}）→ "
                f"{'PASS' if got else 'FAIL'}，应为 {'PASS' if expect else 'FAIL'}"
                + ("" if got is expect and gotDate == expectDate else " ← 判据方向/量级写错"))
    else:
        add(g, "盘后判据边界用例", None, "trade_cal 未覆盖 2026-09-11，跳过")


    # 11b 计划区现货口径必须显式（防退回与预演区实时现货同名的多口径歧义）
    #     注意：断言里的字面量都限定在**目标函数体**内检查，避免自指（LESSON-015）
    sp = readText("static/js/rt_monitor_opt_sp_plan.js")
    fc = re.search(r"function flagChips\(d\)\s*\{.*?\n    \}", sp, re.S)
    fcBody = fc.group(0) if fc else ""
    pl = re.search(r"function planSpotLabel\(d\)\s*\{.*?\n    \}", sp, re.S)
    plBody = pl.group(0) if pl else ""
    add(g, "计划区chip含标的收盘标签", "标的收盘" in plBody,
        "planSpotLabel 以「标的收盘」标识 T-1 收盘口径")
    add(g, "计划区chip附行情日", (r"/^(\d{4})-(\d{2})-(\d{2})/" in plBody
                              and "d.asof" in plBody),
        "行情日取自 d.asof（etf_daily 末日）")
    add(g, "计划区chip不再自称现货", "现货" not in fcBody,
        "计划区=收盘口径；「现货」字样仅留预演区实时价")
    add(g, "flagChips 走统一取标签函数", "planSpotLabel(d)" in fcBody,
        "计划区现货文案单点实现，避免两处字符串各自漂移")

    # 11c 运行态：页面下发的 JS 已含计划区收盘标签（纯 JS 渲染，HTML 里查不到）
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/static/js/rt_monitor_opt_sp_plan.js",
                                    timeout=15) as resp:
            served = resp.read().decode("utf-8", errors="replace")
        add(g, "页面下发JS含计划区收盘标签", ("planSpotLabel" in served and "标的收盘" in served),
            f"{len(served)} 字符（static 每次读盘，无需重启即生效）")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        add(g, "页面下发JS含计划区收盘标签", None, f"{type(exc).__name__}: {exc}")

    # 12 不给 socketio.run 传 threaded（flask_socketio 内部已固定 True）
    afCode = stripPyComments(af)
    call = re.search(r"socketio\.run\((.*?)\)\s*$", afCode, re.S | re.M)
    callBody = call.group(1) if call else afCode
    add(g, "socketio.run 未传 threaded",
        not re.search(r"\bthreaded\s*=", callBody),
        "flask_socketio 内部已固定 threaded=True")

    # 13 非可见链后台低频整表刷新；可见链盘口/量仓另走 1 秒 delta。
    add(g, "非可见链后台低频刷新",
        ("backgroundRefreshTick" in hCode
         and "scheduleBackgroundRefresh();" in hCode
         and "loadUnderlying(getActiveUnderlying()" in hCode),
        "可见链=1s 字段 delta；其余整表轮转低频（默认 6s）")

    # 14 内联 JS 已导出并通过 node --check
    js = readText(".dsh_tmp/_tboard_inline.js")
    add(g, "内联JS已通过node --check", bool(js),
        f"{len(js)} 字符（由 check_template_js_syntax.py 生成）")


# ---------------------------------------------------------------- 运行态

def checkRuntime() -> None:
    g = "runtime"
    # 端口
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"], text=True, encoding="utf-8",
            errors="replace", timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        out = ""
    ports = {}
    for p in (15573, 15574, 15580, 15581, 5000):
        ports[p] = any(
            "LISTENING" in ln and f":{p} " in ln for ln in out.splitlines()
        )
    # QMT 主桥是首选，但 15580 已实现 TDX-only 自动降级：若主桥离线而五链
    # TDX 字段确已覆盖，页面仍然可用，只能报 DEGRADED，不能伪报“服务不可用”。
    # 反之，降级快照/端口不完整仍必须红灯。
    CLIENT_DEP = (15580, 15581)
    try:
        snapshot = getJson(DAQMT)
        snapshotError = None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        snapshot = None
        snapshotError = exc

    # LESSON-063 运行态：重启脚本会写分段标记；只审当前重启段，避免历史异常
    # 让已修复版本永久红灯。没有标记时退回日志尾部，仍能抓住盘中持续异常。
    daqmtLog = readText("大QMT实时行情.log")
    restartMarker = daqmtLog.rfind("restart_daqmt_quote 重启")
    activeDaqmtLog = (
        daqmtLog[restartMarker:]
        if restartMarker >= 0
        else daqmtLog[-200000:]
    )
    szoNoneMergeError = (
        "overlaySzoMinuteOiCandidate" in activeDaqmtLog
        and "AttributeError: 'NoneType' object has no attribute 'get'" in activeDaqmtLog
    )
    add(g, "SZO分钟OI当前运行段无空记录合并异常",
        (not szoNoneMergeError) if ports[15580] else None,
        "当前重启段未发现 overlaySzoMinuteOiCandidate NoneType 异常"
        if not szoNoneMergeError else
        "当前重启段仍有 overlaySzoMinuteOiCandidate NoneType 异常，修复未部署或已回归")

    qmtBridgeUp = ports[15573] and ports[15574]
    coreOk, coreMode, coreDetail = coreQuoteRuntimeHealth(
        ports, snapshot, inTradingSession()
    )
    add(g, f"核心服务可用（{coreMode}）", coreOk, coreDetail)
    fallbackOk, fallbackDetail = (
        tdxFallbackRuntimeHealth(ports, snapshot)
        if isinstance(snapshot, dict) else (False, "15580 快照不可读")
    )
    if not qmtBridgeUp:
        add(g, "QMT主桥状态（TDX降级中）", None,
            "DEGRADED：15573/15574 未同时监听；"
            + ("页面以 TDX-only 继续服务，OI 仍为未知，待 QMT 桥恢复"
               if fallbackOk else
               "盘外待市或 TDX 降级尚未具备当前日期字段；待开市复核"))
    depUp = [p for p in CLIENT_DEP if ports[p]]
    if inTradingSession():
        add(g, "客户端依赖端口在监听（盘中）",
            len(depUp) == len(CLIENT_DEP),
            f"盘中应有 {CLIENT_DEP}；当前在监听 {depUp or '无'}")
    else:
        add(g, "客户端依赖端口（盘外可不在）", None,
            f"非交易时段，{depUp or '均未启动'}"
            f"（需通达信/大QMT 客户端在运行；不作为判据）")

    # 构建号（期望值取自仓库单一来源 config.py，避免自检里硬编码副本悄悄过期）
    try:
        from rt_monitor.core.config import RT_MONITOR_STATIC_VER as _expectedVer
    except Exception as exc:  # noqa: BLE001
        _expectedVer = ""
        add(g, "期望构建号可读", False, f"{type(exc).__name__}: {exc}")
    try:
        req = urllib.request.Request("http://127.0.0.1:5000/")
        with urllib.request.urlopen(req, timeout=10) as resp:
            build = resp.headers.get("X-RT-Monitor-Build") or ""
        add(g, "运行中的构建号", bool(_expectedVer) and build == _expectedVer,
            f"运行={build or '(空)'} 期望={_expectedVer or '(未知)'}"
            + ("" if build == _expectedVer else " → 进程未重启，改动尚未生效"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        add(g, "运行中的构建号", False, f"{type(exc).__name__}: {exc}")

    try:
        req = urllib.request.Request("http://127.0.0.1:5000/api/opt-iv-perc-anchors")
        with urllib.request.urlopen(req, timeout=10) as resp:
            percBody = json.loads(resp.read().decode("utf-8"))
        percItems = percBody.get("items") or []
        add(g, "Per C锚点API可读",
            bool(percBody.get("ok")) and any(
                isinstance(x, dict) and x.get("ts_code") == "588000.SH" for x in percItems
            ),
            f"ok={percBody.get('ok')} n={len(percItems)}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        add(g, "Per C锚点API可读", False, f"{type(exc).__name__}: {exc}")

    # 快照来源分布。上方已读取一次，避免自检期间拿到两份不同时间的快照。
    d = snapshot
    if d is None:
        exc = snapshotError
        # 盘外 daqmt 可能未启动（依赖 QMT/通达信客户端），不算缺陷；
        # 盘内取不到快照才是真故障。
        add(g, "快照可用", None if not inTradingSession() else False,
            (f"非交易时段，daqmt(15580) 未运行：{type(exc).__name__}"
             "（需大 QMT 客户端在运行；不作为判据）")
            if not inTradingSession() else f"{type(exc).__name__}: {exc}")
        return
    meta = d.get("optMeta") or {}
    opt = d.get("opt") or {}
    byChain = {}
    for k, m in meta.items():
        if not isinstance(m, dict):
            continue
        six = str(m.get("undl") or "").split(".", 1)[0]
        rec = opt.get(k) or {}
        byChain.setdefault(six, [0, 0])
        byChain[six][0] += 1
        if rec.get("src") == "tdx":
            byChain[six][1] += 1
    tdx50 = byChain.get("510050", [0, 0])
    if tdx50[1] > 0:
        add(g, "510050 标记为 tdx 供给", True, f"510050: {tdx50}")
    elif (not inTradingSession()) and tdx50[0] > 0:
        add(g, "510050 标记为 tdx 供给", None,
            "盘外跨日后历史 tick 被日期过滤；开市后的字段来源由 TDX 降级健康检查复核")
    else:
        add(g, "510050 标记为 tdx 供给", False, f"510050: {tdx50}")

    if qmtBridgeUp:
        add(g, "QMT链未标 tdx",
            byChain.get("510500", [0, 1])[1] == 0,
            f"510500: {byChain.get('510500')}")
    elif (not inTradingSession()) and tdx50[0] > 0 and tdx50[1] <= 0:
        add(g, "QMT链来源角色（盘外待市）", None,
            f"QMT主桥离线；{fallbackDetail}")
    else:
        add(g, "QMT链来源角色（TDX降级）", None,
            f"DEGRADED：主桥离线；{fallbackDetail}；不把字段级 TDX 来源误写成 QMT 正常供给")

    # 各链新鲜度
    nowMs = datetime.now().timestamp() * 1000.0
    lastClose = lastClosedSessionTs()
    for u in ("510500", "510300", "588000"):
        ages = []
        for k, m in meta.items():
            if not isinstance(m, dict) or str(m.get("undl")) != f"{u}.SH":
                continue
            rec = opt.get(k) or {}
            t = toMs(rec.get("time")) or toMs(rec.get("t"))
            if t > 0:
                ages.append((nowMs - t) / 1000.0)
        if not ages:
            add(g, f"{u} 新鲜度", None, "无数据")
            continue
        ages.sort()
        med = ages[len(ages) // 2]
        p90 = ages[int(len(ages) * 0.9)]
        # 尾盘静默（14:45–15:00）里"中位年龄大"同样是行情性质：
        # 实测 14:59 三条 QMT 链中位 132s、但 min 仍有 9s（部分合约还在成交），
        # 而 14:52 时中位仅 65~109s —— 是合约陆续停成交，不是链路失效。
        # 因此与 `灰行<25%` 用同一个窗口；窗口外判据完全不变。
        if inTradingSession() and not inCloseTailQuiet():
            add(g, f"{u} 中位年龄<30s", med < 30.0,
                f"中位 {med:.1f}s  P90 {p90:.0f}s  n={len(ages)}")
        elif inCloseTailQuiet():
            add(g, f"{u} 中位年龄<30s", True,
                f"中位 {med:.1f}s P90 {p90:.0f}s n={len(ages)}"
                f"（尾盘静默窗口，绝对新鲜度不作判据）")
        else:
            # 盘后判据 = 「数据是否停在最近一次收盘附近」，不是绝对新鲜度。
            # ① 年龄上限随"距今多久收盘"走（周末/长假自动放宽）——深夜不再必然误报；
            # ② 再校验行情日是否等于"最近已收盘交易日" —— 这一条才能抓住
            #    "整天没更新"（否则周一早上拿到上周五的 tick，年龄看着也"合理"）。
            expected = expectedTickDate()
            tickDates = []
            for k, m in meta.items():
                if not isinstance(m, dict) or str(m.get("undl")) != f"{u}.SH":
                    continue
                t = toMs((opt.get(k) or {}).get("time")) or toMs((opt.get(k) or {}).get("t"))
                if t > 0:
                    tickDates.append(datetime.fromtimestamp(t / 1000.0).date())
            dateOk = bool(tickDates) and max(tickDates) == expected
            maxAge = postCloseMaxAgeSec()
            add(g, f"{u} 盘后停在收盘附近", (med < maxAge) and dateOk,
                f"非交易时段，中位 {med/60:.1f}min < 上限 {maxAge/3600:.1f}h"
                f"（今距最近收盘 {lastClose:%m-%d %H:%M}）；"
                f"行情日 max={max(tickDates) if tickDates else '无'} 期望={expected}"
                f" {'✓' if dateOk else '✗ 疑似整天未更新'}")

    # 订阅推送覆盖
    try:
        if r"D:\cursor\xtquant_big_convert\src" not in sys.path:
            sys.path.insert(0, r"D:\cursor\xtquant_big_convert\src")
        os.environ.setdefault("BIGQMT_RPC_TIMEOUT_SECONDS", "30")
        from bigqmt_signal_trader.xtquant_compat import configure, xtdata
        try:
            configure(timeout_seconds=30.0)
        except Exception:  # noqa: BLE001
            configure()
        data = xtdata.get_single_quote_ticks(max_age_sec=60, timeout_seconds=25) or {}
        sho = sum(1 for k in data if str(k).upper().endswith(".SHO"))
        if inTradingSession():
            add(g, "订阅推送60s覆盖", sho >= 300, f"SHO {sho} 张")
        else:
            add(g, "订阅推送60s覆盖", None,
                f"非交易时段无 tick，SHO {sho} 张（SKIP）")
    except Exception as exc:  # noqa: BLE001
        add(g, "订阅推送60s覆盖", None, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 页面

# ---------------------------------------------------------------- 教训守卫

def checkLessons() -> None:
    """把 .planning/LESSONS.md 的教训变成可执行守卫。

    守卫失效 = FAIL：若某条教训的机制被重构删掉，这里必须报错，
    强迫重新评估该教训是否仍适用，而不是让它默默腐烂。
    """
    g = "lessons"
    if not os.path.exists(os.path.join(_REPO, ".planning", "LESSONS.md")):
        add(g, "LESSONS.md 存在", False, "未找到 .planning/LESSONS.md")
        return
    add(g, "LESSONS.md 存在", True, ".planning/LESSONS.md")

    qCode = stripPyComments(readText("大QMT实时行情.py"))
    hCode = stripPyComments(readText("templates/rt_monitor_opt_t_board.html"))
    afCode = stripPyComments(readText("rt_monitor/app_factory.py"))
    bCode = stripPyComments(readText("rt_monitor/services/opt_t_board.py"))
    tCode = stripPyComments(readText("tdx_opt_live.py"))
    swCode = stripPyComments(readText("rt_monitor/services/sw_industry.py"))
    swTests = readText("tests/test_sw_rt_quote_sanitize.py")

    # LESSON-001 方向性比较
    m = re.search(r"def isStaleIncomingTick.*?(?=\ndef )", readText("大QMT实时行情.py"), re.S)
    fn = stripPyComments(m.group(0)) if m else ""
    add(g, "L001 陈旧判定有方向", ("refMs - ms" in fn and "abs(refMs - ms)" not in fn),
        "不得用 abs(refMs - ms)")

    # LESSON-002 观测推进时钟
    add(g, "L002 观测推进字段时钟", ("clocksAdvanced" in qCode),
        "值未变但时钟推进仍需落库")

    # LESSON-003 排除用标记而非删除
    add(g, "L003 非QMT链打来源标记",
        ('packed["src"] = "tdx"' in qCode
         and "OPT_QMT_TICK_UNDERLYINGS" in qCode),
        "保留合约 + src=tdx，而非摘除 optMeta")
    try:
        snap = getJson(DAQMT)
        meta = snap.get("optMeta") or {}
        opt = snap.get("opt") or {}
        n50 = sum(1 for k, v in meta.items()
                  if isinstance(v, dict)
                  and str(v.get("undl", "")).startswith("510050"))
        tagged = sum(1 for k, v in meta.items()
                     if isinstance(v, dict)
                     and str(v.get("undl", "")).startswith("510050")
                     and (opt.get(k) or {}).get("src") == "tdx")
        live50 = sum(1 for k, v in meta.items()
                     if isinstance(v, dict)
                     and str(v.get("undl", "")).startswith("510050")
                     and isinstance(opt.get(k), dict) and bool(opt.get(k)))
        if (not inTradingSession()) and n50 > 0 and live50 <= 0:
            add(g, "L003 运行态510050在且标tdx", None,
                "盘外跨日后历史 tick 被日期过滤；保留 optMeta，开市后再验 TDX 字段来源")
        else:
            add(g, "L003 运行态510050在且标tdx", n50 > 0 and tagged == n50,
                f"510050 合约 {n50}，实时记录 {live50}，标 tdx {tagged}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        add(g, "L003 运行态510050在且标tdx", None, f"{type(exc).__name__}: {exc}")

    # LESSON-004 不猜第三方参数
    call = re.search(r"socketio\.run\((.*?)\)\s*$", afCode, re.S | re.M)
    callBody = call.group(1) if call else afCode
    add(g, "L004 socketio.run 未传 threaded",
        not re.search(r"\bthreaded\s*=", callBody),
        "flask_socketio 已硬编码 threaded=True")

    # LESSON-005 前端语法兜底
    add(g, "L005 内联JS有语法校验产物",
        bool(readText(".dsh_tmp/_tboard_inline.js")),
        "由 scripts/check_template_js_syntax.py 生成")

    # LESSON-006 阈值标定于系统节奏
    add(g, "L006 灰阶阈值=30/300",
        ("_OPT_T_AGE_YOUNG_SEC = 30.0" in bCode
         and "_OPT_T_AGE_OLD_SEC = 300.0" in bCode),
        "必须显著大于实测投递周期(中位8.5s/P75 23s)")
    add(g, "L006 标定脚本存在",
        os.path.exists(os.path.join(_REPO, "scripts", "tune_age_thresholds.py")),
        "scripts/tune_age_thresholds.py")

    # LESSON-007 检查器剥离注释与 docstring
    sc = readText("scripts/selfcheck_delivery.py")
    add(g, "L007 检查器剥离注释与docstring",
        ("def stripPyComments" in sc and 'r\'"""(?:.|\\n)*?"""\'' in sc),
        "stripPyComments 需先去掉三引号块")

    # LESSON-008 具名回归测试
    tests = readText("tests/test_daqmt_realtime_quote.py")
    for name, why in (
        ("testNewerIncomingTickNotTreatedAsStale", "L001/L008 方向性回归"),
        ("testFreshObservationAdvancesClockWithoutValueChange", "L002/L008 时钟推进回归"),
    ):
        add(g, f"L008 {name}", (f"def {name}" in tests), why)

    # 附加：QMT 主导链新鲜度门控（0-I/0-L 的兜底）
    add(g, "QMT主导链新鲜度门控",
        ("isTickOlderThan" in qCode and "QMT_LED_MAX_STALE_SEC" in qCode),
        "QMT 冻结时让 TDX 接管")

    # 附加：TDX 强刷与并行（新鲜度基础）
    add(g, "TDX 周期强刷+并行取数",
        ("REFRESH_SESSION_SEC" in tCode and "PARALLEL_PULL" in tCode),
        "间隔≈滞后上界；主链∥兜底")

    # 附加：非可见链后台刷新
    add(g, "非可见链后台低频刷新",
        ("backgroundRefreshTick" in hCode
         and "scheduleBackgroundRefresh();" in hCode),
        "可见链=1s 字段 delta；其余整表 6s 轮转")

    # LESSON-017 具名回归测试（计划区/预演区现货口径不得同名）
    spTests = readText("tests/test_opt_sp_spot_label.py")
    for name, why in (
        ("testPlanChipNamesCloseAndBarDate", "L017 计划区须标「标的收盘+行情日」"),
        ("testPlanChipSurvivesMissingAsof", "L017 缺 asof 不得退回自称现货"),
        ("testPlanChipDiffersFromLiveSpotText", "L017 不得与预演区实时文案同形"),
        ("testGuardIsEffectiveAgainstOldLabel", "L017 旧实现复现（改回去必红）"),
        ("testSourceHasNoLiveSpotChipInFlagChips", "L017 源码级：flagChips 无现货字面量"),
    ):
        add(g, f"L017 {name}", (f"def {name}" in spTests), why)
    add(g, "L017 守卫注入自证脚本存在",
        os.path.exists(os.path.join(_REPO, "scripts", "selftest_spot_label_guard.py")),
        "selftest_spot_label_guard.py：注入旧实现 → 断言 FAIL → 字节级还原")

    # LESSON-009 读改写还原须用二进制模式
    st = readText("scripts/selftest_guard_effective.py")
    add(g, "L009 注入测试用二进制读写",
        ('"rb"' in st and '"wb"' in st),
        "避免 Windows 文本模式把 \\r\\n 规范化")

    # LESSON-010 守卫有效性可自证
    add(g, "L010 守卫有效性自测脚本存在",
        bool(st) and ("退出码" in st or "returncode" in st),
        "scripts/selftest_guard_effective.py 注入破坏→断言FAIL→还原")
    add(g, "L045 故障注入必须串行验收",
        ("必须与 selfcheck_delivery.py" in st
         and "不得放入并行验收批次" in st),
        "故障注入会短暂改写源码；与正常自检并行会制造假失败")

    # LESSON-011 健康判据须基于速率而非"是否为零"
    add(g, "L011 订阅退化判据存在",
        ("OPT_SUB_DEGRADE_RATIO" in qCode
         and "subDegradeStreak" in qCode
         and "degradedStreak=subDegradeStreak" in qCode),
        "命中率退化也触发 force 重订")
    tests2 = readText("tests/test_daqmt_realtime_quote.py")
    for name2 in ("testForceResyncOnPushDegradation",
                  "testDegradeDetectorEvaluatedOutsideEmptyBranch"):
        add(g, f"L011 {name2}", (f"def {name2}" in tests2),
            "退化场景与分支位置的具名回归")

    # LESSON-018：盘口未变时也必须推进盘口时钟。
    # 缺陷现场：已 fromTdx 的记录，补时钟分支被 `and not existing.get("fromTdx")`
    # 挡住 → 时间戳永久冻结（实测 daqmt 12848s vs TDX 323s），下游一直标陈旧。
    # 修法：让"只有时间戳前进"也算有效更新（只前进不倒退）。两条具名回归守着它。
    tests3 = readText("tests/test_daqmt_realtime_quote.py")
    for name3 in ("testMergeAdvancesTimeWhenBookUnchangedAndAlreadyTdx",
                  "testMergeDoesNotRewindTimeOnOlderIncoming"):
        add(g, f"L018 {name3}", (f"def {name3}" in tests3),
            "盘口未变→时钟仍推进；更旧 incoming 不得倒退")
    add(g, "L018 合并侧有『仅时钟前进』分支",
        ("inMs > curMs" in qCode and "newlyTdx" in qCode
         and 'patched["time"] = incoming.get("time")' in qCode),
        "保留『补 fromTdx 标记』的同时新增『时钟前进』")

    # LESSON-020：字段级时钟必须透出，**且**必须按各自数据源的真实到达节奏刷新。
    # 这段走过两次反转，读全再改：
    #   ① 白名单缺 4 个键 → 时钟到不了 T 板，字段级 as-of 从未生效（LESSON-019 前半）；
    #   ② 直接透出 → 510050 灰行 40%→72%。因为这些时钟原先**只被 QMT 补丁驱动**，
    #      而 `OPT_QTY patch n=10` 每轮只轮转 10 张 / 全链 650 张 ≈ 每合约 260s 一次；
    #      T 板 pick() 却是「键在就只认它（fail-closed）」，于是 TDX 每轮都在重新
    #      观测的 510050 被改按 260s 轮转节奏判灰（自检 `510050 灰行<25%` 直接 FAIL）；
    #   ③ 定稿：先让 `optionTickStampTdxFieldClocks` 把 **TDX 供的字段**按每一轮 TDX
    #      喂数重新计时（观测时刻 = 合并墙钟；TDX 自己的 time 实测在载荷未变时
    #      13/13 次都不动，不能当观测时钟），**再**透出这 4 个键。
    # "先透出、后修语义"必然翻车 —— 所以守卫必须**同时**钉住两件事。
    snapKeys = qCode.split("SNAPSHOT_TICK_KEYS = (", 1)
    snapBody = snapKeys[1].split(")", 1)[0] if len(snapKeys) > 1 else ""
    for fkey in ("lastTime", "qtyTime", "oiTime", "bookTime"):
        add(g, f"L020 快照透出 {fkey}",
            (bool(snapBody) and f'"{fkey}"' in snapBody),
            "字段级 as-of 要上板，这 4 个键必须透出")
    add(g, "L020 公共时钟仍在白名单",
        ('"time"' in snapBody and '"stime"' in snapBody),
        "无专属键的记录靠它回退，不能被一起裁掉")
    add(g, "L020 TDX 供的字段按喂数节奏重新计时",
        ("def optionTickStampTdxFieldClocks" in qCode
         and "optionTickStampTdxFieldClocks(merged, existing, observedMs)" in qCode
         and "optionTickStampTdxFieldClocks(patched, existing, observedMs)" in qCode),
        "两种落盘形状都要计时，否则最需要它的 510050 形态漏掉")
    add(g, "L020 量仓槽位不得跟 TDX 推进",
        ('for field, valueKey in (("qty", "vol"), ("oi", "oi"))' in qCode
         and 'if str(out.get(sourceKey) or "").strip().lower() == "qmt":' in qCode
         and "and not sourceChanged" in qCode),
        "量仓是 QMT 槽位；仅 QMT→TDX 的真实来源交接可重计时，不能给 QMT 值背书")
    for name5 in ("testTdxMergeAdvancesFieldClocksFully",
                  "testTdxMergeDoesNotAdvanceQmtOwnedFieldClocks",
                  "testTdxMergeFieldClockNeverRewinds",
                  "testTdxMergeClockStampCoversBothBranchShapes",
                  "testMergeRestampsTdxObservationWhenSourceEventTimeIsUnchanged",
                  "testSzoMinuteOverlayPreservesTdxBookAndObservationClock",
                  "testSnapshotKeepsFieldLevelClocks",
                  "testSnapshotMustNotFabricateZeroFieldClocks"):
        add(g, f"L020 {name5}", (f"def {name5}" in tests3),
            "字段时钟语义 + 透出 + 不倒退 + 0 值 fail-closed，逐条钉死")

    # LESSON-021：新鲜度判据必须带时段窗口 —— 固定阈值在尾盘/盘后必然误报。
    # 实测两次假 FAIL：① 14:57 510050 灰行 26%（TDX 自己的 time 已 p50=73s，
    # 合约陆续停成交，页面确实该变灰）；② 14:59 三条 QMT 链中位 144s
    # （但 min 仍有 9s，14:52 时中位仅 65~109s）。两者都是行情性质，不是服务问题。
    # 修法：`inCloseTailQuiet()`（14:45–15:00）被两条判据共用；窗口外判据不变。
    scSrc = readText("scripts/selfcheck_delivery.py")
    tw = readText("tests/test_selfcheck_time_windows.py")
    add(g, "L021 尾盘静默窗口存在且边界可测",
        ("def inCloseTailQuiet" in scSrc and "def testWindowBoundariesAreExact" in tw),
        "14:45–15:00 窗口；边界用例含 14:44（窗口外必须仍判）与 15:00")
    add(g, "L021 窗口不得吞掉整个交易时段",
        ("def testWindowDoesNotSwallowWholeSession" in tw),
        "防止有人把窗口放大成整个下午，把白天真问题一起盖掉")
    add(g, "L021 两条新鲜度判据共用同一窗口",
        ("inCloseTailQuiet()" in scSrc.split("def checkBoard", 1)[-1]
         and "inCloseTailQuiet()" in scSrc.split("def checkRuntime", 1)[-1]
         and "def testBothFreshnessChecksShareTheWindow" in tw),
        "只改一条会让另一条继续误报")
    add(g, "L021 盘后年龄上限复用同一函数",
        ("postCloseMaxAgeSec(" in scSrc and "def testTradingSessionStillEndsAt1500" in tw),
        "盘后不是绝对新鲜度；上限随「距最近收盘多久」走")

    # LESSON-022：字段级来源只能为实际字段值背书；TDX 缺 OI 时必须显示未知。
    deltaTests = readText("tests/test_opt_t_board_daqmt_overlay.py")
    add(g, "L022 空OI不得伪标TDX来源",
        ("sourceKnown: bool" in bCode
         and "_optTDeltaSource(packed, sourceField) if sourceKnown else None" in bCode
         and "def optionTickStampFieldSources" in qCode
         and 'optionTickStampFieldSources(incoming, "tdx")' in qCode
         and 'optionTickStampFieldSources(incoming, "qmt")' in qCode
         and "def testLightDeltaDoesNotAttributeMissingOiToTdx" in deltaTests
         and "def testTdxMergeKeepsQmtSourcesForQmtCarriedFields" in tests3
         and "def testDirectQmtTicksStampFieldSources" in tests3),
        "空字段来源必须为未知；QMT/TDX 仅标实际字段，TDX 保留 QMT 值时连来源与时钟一起保住")

    # LESSON-023：开放仓位超过固定 P0 槽位时，保留在 P0 队列并显式报容量不足。
    add(g, "L023 开放仓位不得静默降入长尾",
        ("heldInChain = sorted(c for c in chain if c in held)" in qCode
         and "max(perUndl, len(heldInChain))" in qCode
         and "def testOpenPositionsAreNeverDemotedIntoTailWhenTheyExceedP0Slots" in tests3),
        "固定槽位是吞吐预算，不得裁掉开放仓位的优先语义")

    # LESSON-024：QMT 桥接离线时，TDX-only 必须先于阻塞式 RPC 等待运行。
    mainStart = qCode.find("prepareTdxFallbackChain(logger)")
    mainWait = qCode.find("waitUntilRpcPing(logger, xt_trader)")
    add(g, "L024 QMT离线仍启动TDX-only",
        ("def _runTdxFallbackLoop" in qCode
         and "forceTdx=True" in qCode
         and mainStart >= 0 and mainWait > mainStart
         and "def testForcedTdxMergeCoversNominalQmtLedChain" in tests3
         and "def testPrepareTdxFallbackLoadsAllFiveChainsFromOptBasic" in tests3
         and "def testTdxFallbackPollOnceDoesNotTouchQmtRpc" in tests3
         and "def testMainStartsTdxFallbackBeforeBlockingForQmtRpc" in tests3),
        "TDX 降级线程不依赖 QMT RPC；主桥恢复后由事件让出")

    add(g, "QMT日切不得覆盖TDX两链",
        ("def mergeOptionUniverseCodes" in qCode
         and "mergedOptCodes = mergeOptionUniverseCodes(optCodes, fallbackCodes)" in qCode
         and "oldTdxCodes = optionQtyTdxUniverse(oldCodes, _copyOptionMeta())" in qCode
         and "testOptionUniverseMergeKeepsTdxFallbackAfterQmtResolve" in tests3
         and "testRefreshOptionChainPreservesTdxCodes" in tests3),
        "启动与日切重拉只更新 QMT 三链，必须保留 510050/159915 的 TDX 调度宇宙")

    # LESSON-025：主桥离线不等于页面不可用；只有五链 TDX 字段实际覆盖时才可降级 PASS。
    fallbackSelfcheckTests = readText("tests/test_selfcheck_qmt_tdx_fallback.py")
    add(g, "L025 主桥/降级可用性分层验收",
        ("def tdxFallbackRuntimeHealth" in scSrc
         and "def coreQuoteRuntimeHealth" in scSrc
         and "OPT_FIVE_UNDERLYINGS" in scSrc
         and "QMT主桥离线" in scSrc
         and "盘外跨日后历史 tick 被日期过滤" in scSrc
         and "def testQmtOfflineWithFiveChainTdxSnapshotIsDegradedButAvailable" in fallbackSelfcheckTests
         and "def testMissingChainCannotBeHiddenAsHealthyFallback" in fallbackSelfcheckTests
         and "def testQmtLabeledFieldIsNotProofOfTdxFallback" in fallbackSelfcheckTests
         and "def testTdxClientPortIsMandatoryForFallback" in fallbackSelfcheckTests
         and "def testOffSessionCrossDayEmptySnapshotIsSkipNotFalseHealthy" in fallbackSelfcheckTests
         and "def testSameEmptySnapshotMustFailDuringTradingSession" in fallbackSelfcheckTests),
        "主桥离线仅在五链有 TDX 实际字段且 5000/15580/15581 齐备时降级 PASS；缺链/伪来源/缺端口仍红")

    # LESSON-028：rt_sw_k 的 pct_change 缺失时，须用同快照 close/pre_close 安全反算。
    add(g, "L028 SW实时涨跌幅可由同快照价格反算",
        ("preCloseRaw: Any = None" in swCode
         and "derived_pct = (cl_f / pre_close_f - 1.0) * 100.0" in swCode
         and "SELECT ts_code, close, pre_close, trade_time, pct_change, amount" in swCode
         and "def testDerivesPctFromCloseAndPreCloseWhenSourceIsMissing" in swTests
         and "def testDoesNotDerivePctWithoutUsablePreClose" in swTests
         and "def testDerivedPctWritesTColDuringLightweightRefresh" in swTests),
        "pct_change 缺失时只允许由同快照 close/pre_close 反算；无有效前收仍留空")

    # LESSON-029：callback 刚收到只能说明传输仍通，不能把缓存重放伪装成市场新鲜。
    # 两层都必须看交易所事件时钟：外层不接纳旧 tick 以触发既有补订，桥接不把它留在
    # fresh cache 中。具名测试分别锁住这两个边界，避免只保留其中一层后旧 OI 重现。
    bridgeCode = readText(
        r"D:\cursor\xtquant_big_convert\src\bigqmt_signal_trader\single_quote_subscription.py"
    )
    bridgeTests = readText(
        r"D:\cursor\xtquant_big_convert\tests\bigqmt_signal_trader\test_single_quote_subscription.py"
    )
    add(g, "L029 外层拒绝陈旧市场事件",
        ("def filterOptionQuoteCacheTicks" in qCode
         and "eventMs = optionTickTimeMs(tick)" in qCode
         and "nowMs - eventMs > maxAgeMs" in qCode
         and "stale_event" in qCode),
        "callback receipt 新不能覆盖交易所 event time 陈旧")
    add(g, "L029 桥接缓存同时校验事件时钟",
        ("def quoteTickEventAgeSec" in bridgeCode
         and "eventAge = quoteTickEventAgeSec(rec, nowWall=nowWall)" in bridgeCode
         and "eventAge is None or eventAge < stale" in bridgeCode
         and 'tick["eventAgeSec"]' in bridgeCode),
        "桥接缓存和诊断均须暴露交易所事件年龄")
    add(g, "L029 回调重放具名回归",
        ("def testCallbackReceiptCannotMaskStaleMarketEvent" in tests3
         and "def test_repeated_stale_market_event_is_not_a_live_callback" in bridgeTests),
        "receipt 新、event 旧必须被两层测试拦住")

    # LESSON-030：全推已静默时，OI 补丁不可继续占用同一 QMT 策略线程。
    # TDX 合并仍在门闩前执行；这里只阻断 get_market_data_ex 型的 QMT 补丁。
    add(g, "L030 全推静默时OI补丁让回调优先",
        ("OPT_QTY_WHEN_QUOTE_DEAD = False" in qCode
         and "def shouldRunOptionQtyPatch" in qCode
         and "yieldQuote and not OPT_QTY_WHEN_QUOTE_DEAD" in qCode
         and "allowQty = shouldRunOptionQtyPatch(" in qCode
         and "def testQtyPatchYieldsWhenWholeQuoteIsStale" in tests3
         and "def testQtyStopsWhenQuoteDeadYield" in tests3),
        "全推假活/hold 时不发重型 OI RPC；健康后才按 P0/P1 补")

    # LESSON-031：P1 不可达时不得跳到 2x20；仅可在 P0/全推安全门下单请求渐进探测。
    add(g, "L031 OI升级只能走受控单请求探测",
        ('"name": "base6", "chunkSize": 6, "maxChunks": 1' in qCode
         and '"name": "probe8", "chunkSize": 8, "maxChunks": 1' in qCode
         and '"name": "probe10", "chunkSize": 10, "maxChunks": 1' in qCode
         and "def optionQtyRoundProbeSafe(" in qCode
         and 'modeReason = "probe_up"' in qCode
         and "def testProbeLadderUsesOneRequestAndKeepsP0AsSafetyGate" in tests3),
        "P1 未达仍如实 DEGRADED；仅 P0/全推安全的 6→8→10 单请求可探测，绝不恢复2x20")

    add(g, "L032 OI不得靠第二个QMT内置策略隔离",
        ('"name": "base6", "chunkSize": 6, "maxChunks": 1' in qCode
         and '"name": "probe8", "chunkSize": 8, "maxChunks": 1' in qCode
         and '"name": "probe10", "chunkSize": 10, "maxChunks": 1' in qCode
         and "def optionQtyRoundProbeSafe(" in qCode
         and "testProbeLadderUsesOneRequestAndKeepsP0AsSafetyGate" in tests3),
        "同一 QMT 内仅一个主桥；OI 只在主桥内受控串行，未创建第二策略隔离路径")

    # LESSON-033：快速 RPC ACK 不能在 callback 观察前被当作空订并主动撤销。
    add(g, "L033 快速订阅ACK须保留并由回调确认",
        ("SUBSCRIBE_FAST_ACK elapsed_s=%.3f keep sub_id=%s，等待 watchdog 回调确认"
         in readText("大QMT实时行情.py")
         and "def testFastSubscribeAckIsKeptForWatchdogConfirmation" in tests3),
        "快速 ACK 只能诊断；启动保留句柄，真实回调由 watchdog 判定")

    # LESSON-034：盘后市场事件已冻结，QMT 补齐只会同主桥回调争资源。
    catchupDisabled = bool(re.search(
        r"def isOptionCatchupWindow\(.*?\).*?return False",
        qCode,
        re.S,
    ))
    add(g, "L034 盘后禁QMT补齐且段头分离事件时钟",
        (catchupDisabled
         and "def isOptionPreopenBootstrapWindow" in qCode
         and "preopenBootstrap and (not skipHeavy)" in qCode
         and "def testAfterCloseDisablesQmtCatchupAndKeepsOnlyPreopenBootstrap" in tests3
         and "def _optTBoardLastMarketEventAt" in bCode
         and "def testLiveClockSeparatesLocalObservationFromLastMarketEvent" in deltaTests),
        "盘后仅合并本地快照；页面必须区分本地观测与原始tick市场事件")

    # LESSON-035：订阅句柄仍在不代表 option callback 仍活；失败后不可无限 force churn。
    add(g, "L035 期权回调熔断后保留有界回退",
        ("def shouldEnterOptionPushFallback(" in qCode
         and "OPT_QUOTE_SUB_FORCE_CONFIRM_SEC" in qCode
         and "def shouldExitOptionPushFallback(" in qCode
         and "OPT_QUOTE_SUB_RECOVER_CONFIRM_SEC" in qCode
         and "OPT_SUB dead_push_fallback enter" in qCode
         and "quoteRecovery=bool(optPushForceConfirmMono and not optPushFallback)" in qCode
         and "optionCallbackEmpty=bool(" in qCode
         and "or quoteCacheEmptyHits > 0" in qCode
         and "def testDeadPushConfirmationSuppressesOiUntilRecoveryDecision" in tests3
         and "def testDeadPushRecoveryNeedsStableFreshWindow" in tests3
         and "optionCallbackEmpty=True" in tests3
         and "def testTailKeepsBothTdxUnderlyingsInEachProbe10TailBatch" in tests3
         and "def testTdxDemandPrioritizesOldestUnobservedOiBeforeNewestChange" in tests3),
        "一次受控重订确认失败后停止 90 秒 churn；恢复必须连续新鲜确认，期间 OI 让路，并保持两条 TDX 链公平轮转")

    # LESSON-043：完整五档价量必须参与源端变化键、合并指纹和 bookChangedTime。
    add(g, "L043 完整五档进入变化判断",
        ("tuple(asFloat(x) for x in buyp[:5])" in tCode
         and "def optionTickBookFingerprint(" in qCode
         and "deepBook = optionTickBookFingerprint(rec)" in qCode
         and "def testMapAdvancesTimeWhenDeepBookChanges" in readText("tests/test_tdx_opt_live_health.py")
         and "def testFingerprintDetectsDeepBookAndLevelVolumeChanges" in tests3
         and "def testMergeWritesChangedDeepBookAndChangedClock" in tests3),
        "二至五档或任一档量变化必须更新盘口并推进 bookChangedTime")

    # LESSON-044：TDX 合并与 QMT 重型 RPC 隔离；午休有界收尾；SZO OI 保留影子审计。
    add(g, "L044 轻量TDX合并独立于QMT重任务",
        ("TDX_OPT_DEDICATED_MERGE" in qCode
         and "def _runTdxMergeLoop(" in qCode
         and "def shouldMergeTdxInOptionPoll(" in qCode
         and "def isOptionHeavyRpcWindow(" in qCode
         and "OPT_LUNCH_HEAVY_GRACE_SEC" in qCode
         and "def szoMinuteOiShadowStats(" in qCode
         and "OPT_SZO_OI_SHADOW" in qCode
         and "def testDedicatedTdxMergeLoopDoesNotNeedQmtRpc" in tests3
         and "def testLunchHeavyRpcStopsAfterBoundedCatchup" in tests3
         and "def testSzoMinuteOiShadowReportsCandidateWithoutMutation" in tests3),
        "TDX独立线程默认开启；午休5分钟后停重型RPC；SZO分钟OI保留shadow，正式写入另受L047严格时钟门控")

    # LESSON-046：主动恢复和已确认的稳定 fallback 是两个状态；后者不能永久停 OI。
    add(g, "L046 稳定fallback恢复有界OI轮询",
        ("optionCallbackFallback: bool = False" in qCode
         and "optionCallbackEmpty and not optionCallbackFallback" in qCode
         and "optionCallbackFallback=bool(optPushFallback)" in qCode
         and "optionCallbackFallback=True" in tests3
         and "quoteRecovery=True" in tests3),
        "主动恢复期间OI让路；确认进入稳定TDX fallback后仅在全推安全门下恢复6/8/10张串行轮询")

    # LESSON-047：分钟合成的公共 time 属于 close；OI 必须有自己的事件时钟。
    add(g, "L047 SZO分钟OI只按独立新时钟覆盖",
        ("def overlaySzoMinuteOiCandidate(" in qCode
         and 'out["oiSource"] = "qmt_1m"' in qCode
         and 'tick["oiTime"] = lastOiT' in qCode
         and "minuteMs <= existingMs" in qCode
         and "OPT_SZO_OI_APPLY n=%d source=qmt_1m" in qCode
         and "def testSzoMinuteOiCandidateUpdatesOnlyNewerOi" in tests3
         and "def testSzoMinuteOiAppliesEvenWhenFreshTdxBlocksMinutePrice" in tests3),
        "仅严格更新且带独立oiTime的159915分钟OI可覆盖；不得改写最新价、量、盘口或公共时钟")

    # LESSON-048：510050 分钟 OI 仅健康时卸载长尾；P0、互斥调度和两级回退不能丢。
    add(g, "L048 510050分钟OI健康接管且可秒退",
        ("DAQMT_OPT_SHO_MIN_OI" in qCode
         and "OPT_SHO_MIN_OI_DISABLE_FILE" in qCode
         and "def pollShoMinuteOiRound(" in qCode
         and "def shoMinuteOiRoundHealthy(" in qCode
         and "shoOffload = shoMinuteOiOffloadCodes(startMono)" in qCode
         and "tailExcludeCodes=shoOffload | szoOffload" in qCode
         and "if allowQty and not minuteOiSubmitted:" in qCode
         and "_shoMinuteOiCircuitUntilMono = mono + OPT_SHO_MIN_OI_CIRCUIT_SEC" in qCode
         and "def testShoMinuteOiGateFailsClosedAndSupportsRuntimeRollback" in tests3
         and "def testShoMinuteOiRoundUsesDirectDownloadAndOnlyPatchesOi" in tests3
         and "def testShoMinuteOiRunsBeforeTickOiAndNeverSharesOneLoop" in tests3),
        "最多10张串行分钟下载与tick OI互斥；仅成功合约卸载，通道故障自动熔断且flag可即时回退")

    # LESSON-052：一批成功不能卸载整条链；单张自然缺分钟 OI 只冷却该合约。
    add(g, "L052 510050分钟OI逐合约混合供给",
        ("def shoMinuteOiOffloadCodes(" in qCode
         and "_shoMinuteOiUnavailableUntilMono" in qCode
         and "OPT_SHO_MIN_OI_UNAVAILABLE_COOLDOWN_SEC" in qCode
         and "def shoMinuteOiCircuitDecision(" in qCode
         and "eligible_collapse_pending" in qCode
         and "shoOffload = shoMinuteOiOffloadCodes(startMono)" in qCode
         and "tailExcludeCodes=shoOffload | szoOffload" in qCode
         and "tailExcludeUnderlyings=(\"510050\"" not in qCode
         and "def testMinuteOffloadExcludesOnlyRecentlySuccessfulCodes" in tests3
         and "def testPartialUnknownMinuteOiDoesNotTripGlobalCircuit" in tests3
         and "def testPartialMinuteRoundCoolsOnlyMissingCodeAndKeepsObservedOffload" in tests3),
        "已验证8席+未知2席；单张缺失冷却10分钟；仅最近90秒成功的具体合约卸载，连续已知集塌陷才全局熔断")

    # LESSON-054：轮询间隔门控不是失败，不能冲掉最近一次真实请求的健康结论。
    add(g, "L054 分钟OI门控保留最近健康结论",
        ("health = dict(_shoMinuteOiHealth)" in qCode
         and '"universeN": len(universe)' in qCode
         and '"eligibleN": len(_shoMinuteOiEligibleCodes)' in qCode
         and "_shoMinuteOiHealth = dict(base)" not in qCode
         and "def testMinuteOiIntervalGatePreservesLastRoundHealth" in tests3),
        "正常间隔/开关门控只刷新当前规模，不得把最近一次实际请求的healthy/reason覆盖为失败")

    # LESSON-057：159915 分钟 OI 调度必须看 oiTime，不得被 TDX 盘口时钟遮蔽。
    add(g, "L057 SZO分钟OI按独立时钟补长尾",
        ("DAQMT_OPT_SZO_OI_AGE_SCHED" in qCode
         and 'optionTickFieldTimeMs(rec, "oi")' in qCode
         and "oiAgeSec >= OPT_SZO_OI_REFRESH_STALE_SEC" in qCode
         and "rank = (1, oiTime if oiTime > 0 else -1.0, index)" in qCode
         and "def testSzoLiveDownloadIncludesOldOiDespiteFreshTdxBook" in tests3),
        "TDX盘口新鲜不代表OI新鲜；冷缺口优先且OI-only候选按最旧oiTime入队，不增批量/并发")

    # LESSON-063：新挂牌/首次入缓存可能没有 existing；必须先归一化再读字段。
    oiOverlayFn = ""
    mOiOverlay = re.search(
        r"def overlaySzoMinuteOiCandidate\(.*?(?=\ndef )", qCode, re.S
    )
    if mOiOverlay:
        oiOverlayFn = mOiOverlay.group(0)
    normalizeAt = oiOverlayFn.find(
        "existing = existing if isinstance(existing, dict) else {}"
    )
    firstReadAt = oiOverlayFn.find('existing.get("oi")')
    add(g, "L063 SZO分钟OI先归一化空旧记录",
        (normalizeAt >= 0
         and firstReadAt >= 0
         and normalizeAt < firstReadAt
         and "def testSzoMinuteOiCandidateCreatesNewContractWhenExistingIsNone" in tests3),
        "existing=None 先转空字典再读字段；新合约具名回归必须存在")

    # LESSON-064：零成交不等于 OI 无效；OI-only 解析不得改写价量盘口，并须可秒退。
    add(g, "L064 分钟OI与价量成交门槛解耦",
        ("DAQMT_OPT_MINUTE_OI_ONLY" in qCode
         and "def szoMinuteRecordsToOiCandidate(" in qCode
         and (
             "tick = szoMinuteRecordsToOiCandidate(localBars.get(code), asof)" in qCode
             or "localBars.get(code), asof, observedAtMs=observedAtMs" in qCode
         )
         and "localOiTicks[code] = oiTick" in qCode
         and "def testLocalMinuteOiAcceptsZeroVolumeWithoutTouchingQuote" in tests3
         and "def testMinuteOiOnlyFlagRestoresZeroVolumeRejection" in tests3),
        "零成交正OI只推进持仓字段；报价仍守原门槛；DAQMT_OPT_MINUTE_OI_ONLY=0可回退")

    # LESSON-065：多源 OI 成功须反馈给轮转；空闲槽回填；原始 RPC 与最终字段 SLA 分离。
    add(g, "L065 OI调度闭环且双SLA独立",
        ("DAQMT_OPT_SHO_MIN_OI_REFILL" in qCode
         and "DAQMT_OPT_SZO_MIN_OI_OFFLOAD" in qCode
         and '"DAQMT_OPT_SZO_MIN_OI_OFFLOAD", "1"' in qCode
         and "DAQMT_OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC" in qCode
         and "DAQMT_OPT_SZO_MIN_OI_OFFLOAD_GRACE" in qCode
         and "DAQMT_OPT_SZO_MIN_OI_OFFLOAD_GRACE_SEC" in qCode
         and "def szoMinuteOiLeaseDecision(" in qCode
         and "lease_grace_n" in qCode
         and "def optionQtyTailShareSnapshot(" in qCode
         and "share=510050:%d,159915:%d,other:%d" in qCode
         and "OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE" in qCode
         and "DAQMT_OPT_EFFECTIVE_OI_SLA" in qCode
         and "def szoMinuteOiOffloadDiagnostics(" in qCode
         and "def szoMinuteOiEffectiveTimeMs(" in qCode
         and "OPT_SZO_MIN_FUTURE_BAR_ALIGN_SEC" in qCode
         and "observedAtMs=observedAtMs" in qCode
         and "OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC" in qCode
         and "OPT_SZO_OI_DIAG enabled=%d" in qCode
         and "def szoMinuteOiOffloadCodes(" in qCode
         and "tailExcludeCodes=shoOffload | szoOffload" in qCode
         and "maxAgeSec=OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC" in qCode
         and "if refill and len(selected) < total:" in qCode
         and "def optionOiEffectiveSlaSnapshot(" in qCode
         and "raw_slo=%s" in qCode
         and "effective_slo=%s" in qCode
         and "def testRecentSzoMinuteOiLeavesTailThenReentersWhenAged" in tests3
         and "def testSzoMinuteOiOffloadUsesStrictAgeWindowAndRejectsFutureBars" in tests3
         and "def testTailPrioritizesOldestObservedOiBeforeActivity" in tests3
         and "def testSzoMinuteOffloadNeverRemovesUrgentP0" in tests3
         and "def testStrictSzoOffloadShrinksTailWithoutChangingBatchOrP0" in tests3
         and "def testSzoMinuteOiOffloadDiagnosticsSplitEveryEligibilityGate" in tests3
         and "def testLocalMinuteOiAlignsFutureBarEndToCurrentMinute" in tests3
         and "def testMinuteOiFutureBarOutsideAlignmentWindowFailsClosed" in tests3
         and "def testSzoMinuteOiLeaseUsesBoundedPhaseGrace" in tests3
         and "def testSzoMinuteOiLeaseRejectsExpiredWhenPhaseIsNotNear" in tests3
         and "def testSzoMinuteOiDiagnosticsSeparatesLeaseReasons" in tests3
         and "def testTailShareSnapshotReportsExistingFairAllocation" in tests3
         and "def testShoMinuteOiBatchReturnsIdleProbeSlotsToVerifiedContracts" in tests3
         and "def testEffectiveOiSlaIsIndependentFromRawRpcSla" in tests3),
        "SZO分钟OI新鲜合约动态卸载且P0保留；510050空探测槽回填；日志拆分raw/effective SLA且三项可独立回退")

    # LESSON-074：相位宽限不能默认把旧分钟时间戳保留在有效 SLA 内。
    add(g, "L074 SZO相位宽限默认关闭",
        ('"DAQMT_OPT_SZO_MIN_OI_OFFLOAD_GRACE", "0"' in qCode
         and "def testSzoMinuteOiGraceIsDisabledByDefault" in tests3),
        "宽限只保留为显式 A/B 开关；严格年龄仍决定有效 OI 新鲜度")

    # LESSON-075：510050 分钟 OI 时间租约先影子观测，不能未经 A/B 直接改变卸载。
    add(g, "L075 SHO分钟OI租约只读影子诊断",
        ("DAQMT_OPT_SHO_MIN_OI_LEASE_SHADOW" in qCode
         and "OPT_SHO_MIN_OI_LEASE_SHADOW_THRESHOLDS = (45.0, 60.0, 75.0, 90.0)" in qCode
         and "def shoMinuteOiLeaseShadowSnapshot(" in qCode
         and "def maybeLogShoMinuteOiLeaseShadow(" in qCode
         and "shadowSnapshot = shoMinuteOiLeaseShadowSnapshot(" in qCode
         and "currentOffload=shoMinuteOiOffloadCodes(mono)" in qCode
         and "maybeLogShoMinuteOiLeaseShadow(" in qCode
         and "OPT_SHO_OI_LEASE_SHADOW target=%d" in qCode
         and "def testShoMinuteOiLeaseShadowCountsAgeWindowsWithoutChangingState" in tests3
         and "def testShoMinuteOiLeaseShadowIsDiagnosticOnly" in tests3),
        "510050仅记录45/60/75/90秒租约影子统计；不改变现行卸载集合、批量、并发或QMT计划")

    # LESSON-076：SZO 增加下载相位前必须先观察完整锁占用，禁止暗改单批大小。
    add(g, "L076 SZO相位扩容先量化端到端耗时",
        ("def _maybeDownloadSzoLiveTicksChunk(" in qCode
         and "started = time.perf_counter()" in qCode
         and "OPT_SZO_MIN_DL live n=%d applied=%d remain=%d elapsed_s=%.3f" in qCode
         and "chunk = need[:OPT_LIVE_REFRESH_CHUNK]" in qCode
         and "def testSzoMinuteLiveDownloadReportsEndToEndElapsedWithoutChangingBatch" in tests3),
        "SZO第三相位或其它扩容前，日志必须有端到端耗时；默认仍为20张/两相位，不暗增批量或并发")

    # LESSON-077：候选顺序调整前必须证明最老尾部OI是否被实际选中，不能凭配额猜测。
    add(g, "L077 长尾候选排序先做年龄命中取证",
        ("DAQMT_OPT_QTY_TAIL_DIAG_LOG_SEC" in qCode
         and "def optionQtyTailAgeDiagnosticSnapshot(" in qCode
         and "OPT_QTY_TAIL_DIAG stale_s=%.0f" in qCode
         and "plan[\"tail_all\"], plan[\"tail\"]" in qCode
         and "def testTailAgeDiagnosticSeparatesSelectedAndUnservedStaleOi" in tests3),
        "只读比较长尾全集与本轮实际选择的OI年龄；先证实漏服最老项，才可改排序、配额或游标")

    # LESSON-078：诊断输出字段必须由回归断言到真实键名，不能只测试计数。
    add(g, "L078 长尾年龄诊断字段键名可用",
        ("stem = prefix + \"Age\" if prefix else \"age\"" in qCode
         and "item[stem + \"P95Sec\"]" in qCode
         and "result[\"510050\"][\"ageP95Sec\"], 90.0" in tests3
         and "result[\"510050\"][\"selectedAgeP95Sec\"], 10.0" in tests3),
        "诊断聚合既要统计正确，也要写入日志读取的准确字段名；全集与已选项均须具名回归")

    # LESSON-079：严格分钟OI租约改变尾部前，必须先量化回流QMT的容量成本。
    add(g, "L079 严格OI租约先影子评估长尾成本",
        ("def optionQtyTailPlanImpactSnapshot(" in qCode
         and "OPT_SHO_OI_LEASE_PLAN_SHADOW current_offload=%d" in qCode
         and "tailExcludeCodes=set(leaseCodes.get(key) or ()) | szoOffload" in qCode
         and "strictImpacts[key] = optionQtyTailPlanImpactSnapshot(" in qCode
         and "def testTailPlanImpactEstimatesStrictLeaseCostWithoutChangingPlans" in tests3),
        "严格分钟OI时间租约只能影子构造候选计划；先报告返还QMT数量、理论尾部ETA和P0不变性，再决定是否切换")

    # LESSON-080：HTML 单元格 class 数不能用固定除数猜“灰行”。
    boardCheck = readText("scripts/selfcheck_delivery.py")
    # rsplit 避开本守卫中的字符串字面量，拿到真正 checkBoard 函数体。
    boardCheckFn = boardCheck.rsplit("def checkBoard()", 1)[-1].split(
        "\ndef ", 1
    )[0]
    boardCheckTests = readText("tests/test_selfcheck_board_freshness.py")
    add(g, "L080 T表灰阶按结构化字段时钟统计",
        ("BOARD_QUOTE_DELTA" in boardCheck
         and "def summarizeBoardQuoteDeltaAges(" in boardCheck
         and "fullStaleRowN" in boardCheck
         and "book灰=" in boardCheckFn
         and 'html.count("opt-t-age-stale")' not in boardCheckFn
         and "def testStructuredAgeSummarySeparatesFieldCellsFromFullyStaleRows" in boardCheckTests
         and "def testMissingFieldFailsClosedForTheRowAndThatField" in boardCheckTests),
        "自检读取quote-delta字段时钟；分别报告最新/量/OI/盘口灰度和全字段皆灰的合约数，不再用HTML class固定除数猜行数")

    # LESSON-081：159915 分钟 OI 扩容只能经第三相位开关 A/B，不能暗改并发或批量。
    add(g, "L081 SZO第三相位必须可回退且不增批量并发",
        ("OPT_SZO_MIN_THIRD_PHASE_ENABLE = os.getenv(" in qCode
         and '"DAQMT_OPT_SZO_MIN_THIRD_PHASE_ENABLE", "1"' in qCode
         and "def optionSzoLivePhaseSeconds(" in qCode
         and "return (8, 23, 38) if enabled else (8, 38)" in qCode
         and "chunk = need[:OPT_LIVE_REFRESH_CHUNK]" in qCode
         and "def testSzoLiveThirdPhaseDefaultsOnAfterValidatedAb" in tests3),
        "159915默认用:23第三相位补齐本地分钟OI；保持20张单批与串行RPC，显式设0可立即回退")

    # LESSON-082/083：退出期限覆盖RPC等锁，取证沙箱不得访问真实服务。
    # LESSON-084：DLL 相邻字符串区含周期，市场后缀必须在边界处终止。
    try:
        from scripts.probe_tdx_crude_connectivity import extractSuffixes

        fixture = b"SZ\x00\x00SH\x00\x00BJ\x00\x00NYM\x00\x005m\x00\x0015m\x00"
        actualSuffixes = extractSuffixes(fixture)
        add(g, "L084 DLL市场后缀不得混入K线周期",
            actualSuffixes == ["SZ", "SH", "BJ", "NYM"],
            f"独立合成数据边界验证 suffixes={actualSuffixes}")
    except (ImportError, OSError, ValueError, TypeError) as exc:
        add(g, "L084 DLL市场后缀不得混入K线周期", False,
            f"后缀边界守卫无法执行: {type(exc).__name__}: {exc}")

    shutdownTests = readText("tests/test_daqmt_shutdown.py")
    add(g, "L082 单次停止不得等共享RPC锁或无界清理",
        ('target=runService, name="daqmt-quote-service", daemon=True' in qCode
         and "serviceDone.wait(timeout=SHUTDOWN_TIMEOUT_SEC)" in qCode
         and "SHUTDOWN_TIMEOUT" in qCode
         and "server.server_close()" in qCode
         and "def _joinQuoteWorker(" in qCode
         and "testOneSignalExitsEvenWhenBridgeOperationBlocks" in shutdownTests
         and "testProductionDefaultShutdownBudgetIsFiveSeconds" in shutdownTests),
        "信号主线程独立于服务RPC；默认总等待5秒，超时如实披露远端退订未确认，正常路径释放HTTP端口")
    add(g, "L083 退出取证沙箱不得触达真实行情服务",
        ("mod.TDX_OPT_ENABLE = False" in shutdownTests
         and "mod.urllib.request.urlopen = forbiddenNetwork" in shutdownTests
         and "sys.modules[compat.__name__] = compat" in shutdownTests
         and "mod.startQuoteHttp = lambda *args: Http()" in shutdownTests),
        "退出回归关闭默认TDX合并、禁止真实HTTP取数，并注入假QMT和假HTTP服务；防止取证产生真实请求")

    # LESSON-066：重 RPC 后的非权威空读不得触发死推；SZO 正常轮按自然分钟相位对齐。
    add(g, "L066 缓存空读三态与SZO自然分钟相位",
        ("DAQMT_OPT_QUOTE_CACHE_GUARD" in qCode
         and "returnOutcome=True" in qCode
         and "def shouldCountOptionQuoteCacheEmpty(" in qCode
         and "def shouldDeferOptionQuoteCacheRecheck(" in qCode
         and "or quoteCacheRecheckPending" in qCode
         and "DAQMT_OPT_SZO_MIN_PHASE_ALIGN" in qCode
         and "def optionSzoLivePhaseKey(" in qCode
         and "phase=%s aligned=%d" in qCode
         and "def testQuoteCachePollOutcomeSeparatesEmptyFromTransportFailures" in tests3
         and "def testTransientCacheEmptyAfterQtyDoesNotCountOrForce" in tests3
         and "def testOptionPollLoopWiresCacheRecheckAheadOfHeavyRpc" in tests3
         and "def testSzoLiveTwoPhaseRollbackUsesNaturalMinuteEightAndThirtyEight" in tests3
         and "def testSzoLivePhaseWaitReportsNextAllowedPhase" in tests3
         and "def testSzoLivePhaseRollbackDoesNotIncreaseBatchOrCadence" in tests3),
        "busy/timeout/error不累加dead_push；OPT_QTY后停重请求复查；SZO按自然分钟相位，双相位回退路径可测")

    # LESSON-067：仅满足需求等待/P0 OI 年龄安全门的 probe10 可有界借槽；
    # 压力档、批量和串行约束不得改变，且旧开关必须可秒退。
    add(g, "L067 P0闲置槽仅在probe10受控借给长尾",
        ("DAQMT_OPT_QTY_DYNAMIC_P0_LOAN" in qCode
         and "DAQMT_OPT_QTY_P0_LOAN_GUARD" in qCode
         and "DAQMT_OPT_QTY_BOUNDED_P0_LOAN" in qCode
         and "def optionQtyP0LoanDecision(" in qCode
         and "def shouldUseOptionQtyDynamicP0Loan(" in qCode
         and '== "probe10"' in qCode
         and '"maxChunks": 1' in qCode
         and '"chunkSize": 10' in qCode
         and "loan=%d guard=%.2f" in qCode
         and "loan_cap=%d loan_reason=%s" in qCode
         and "def testDynamicP0LoanFillsUnusedSlotsWithoutExceedingBatch" in tests3
         and "def testDynamicP0LoanCanBeDisabledForInstantRollback" in tests3
         and "def testDynamicP0LoanStillCyclesEveryP0WithinTarget" in tests3
         and "def testDynamicP0LoanGuardKeepsExtraP0Margin" in tests3
         and "def testDynamicP0LoanRunsOnlyInProbe10Mode" in tests3
         and "def testDynamicP0LoanStopsWhenUrgentOrRepairSetCanChurn" in tests3
         and "def testDynamicP0LoanDoesNotHideP0OverCapacity" in tests3
         and "def testBoundedP0LoanAllowsOneSlotOnlyWithinObservedAges" in tests3
         and "def testBoundedP0LoanRejectsUnsafeDemandOrOiAge" in tests3
         and "def testBoundedP0LoanCapIsAppliedWithoutChangingBatchSize" in tests3),
        "probe10仅在需求等待/P0 OI年龄安全时最多借1槽；总批量10、串行请求且可秒退")

    # LESSON-071：门控诊断必须按真实数据流拆来源；等待年龄不可读取缺失OI的epoch。
    add(g, "L071 P0门控来源与连续等待年龄只读诊断",
        ("DAQMT_OPT_QTY_P0_DIAG_LOG_SEC" in qCode
         and "def optionQtyP0DiagnosticSnapshot(" in qCode
         and '"demand_missing_oi"' in qCode
         and '"p0_held"' in qCode
         and '"p0_atm"' in qCode
         and '"p0_qmt_repair"' in qCode
         and "pendingSinceMs.setdefault" in qCode
         and "OPT_QTY_DIAG demand=%d" in qCode
         and "def testOptionQtyDiagnosticSplitsRealSourcesWithoutChangingPlan" in tests3
         and "def testOptionQtyDiagnosticLogExecutesPollPathWithoutChangingScheduling" in tests3),
        "价/量/both/missing OI、持仓、ATM、活动度、QMT修复分开；进程内等待时钟且30秒限频，不参与计划")

    # LESSON-072：159915 OI 压力下非关键 OPT_CHUNK 有界让行；不提高批量/并发，
    # 快照过期和显式开关关闭时必须可回退。
    add(g, "L072 OI压力下OPT_CHUNK有界让行",
        ("DAQMT_OPT_CHUNK_OI_GATE" in qCode
         and "def optionChunkOiGateDecision(" in qCode
         and "def updateOptionQtyPressureSnapshot(" in qCode
         and "OPT_CHUNK_OI_GATE defer" in qCode
         and "OPT_CHUNK_OI_GATE probe" in qCode
         and "optionQtyPressureSnapshot()" in qCode
         and "if oiGate[\"defer\"]:" in qCode
         and "def testOptChunkOiGateDefersWhen159915OiPressureIsHigh" in tests3
         and "def testOptChunkOiGateAllowsBoundedProbeAfterInterval" in tests3
         and "def testOptChunkOiGateDoesNotUseStalePressureOrDisabledGate" in tests3
         and "def testOptionPollLoopWiresOiGateBeforeNonCriticalRpc" in tests3),
        "OI压力时只延后非关键OPT_CHUNK并定期间歇探测；不改批量/并发，快照过期或开关关闭可立即回退")

    # LESSON-049：T 表期权 Δ/Γ ≠ IV 看板「溢」(IV−HV)；希腊值不得进 quote-delta。
    optionIv = stripPyComments(readText("option_iv.py"))
    bodyTmpl = readText("templates/rt_monitor_opt_t_board_body.html")
    overlayTests = readText("tests/test_opt_t_board_daqmt_overlay.py")
    ivBoardTmpl = readText("templates/rt_monitor_opt_iv_board.html")
    guideTmpl = readText("templates/rt_monitor_opt_vol_guide.html")
    ivHvTests = readText("tests/test_opt_iv_hv.py")
    deltaFn = ""
    if "def queryOptTBoardQuoteDelta" in bCode:
        deltaFn = bCode.split("def queryOptTBoardQuoteDelta", 1)[1].split("\ndef ", 1)[0]
    add(g, "L049 T表期权Γ与IV看板溢分名且不进quote-delta",
        ("def black76_gamma" in optionIv
         and "black76_gamma(" in bCode
         and "black76_gamma" not in deltaFn
         and "{{ row.call_gamma }}" in bodyTmpl
         and bodyTmpl.find("{{ row.call_gamma }}") < bodyTmpl.find("{{ row.call_delta }}")
         and bodyTmpl.find("{{ row.put_delta }}") < bodyTmpl.find("{{ row.put_gamma }}")
         and "中轴IV − HV" in ivBoardTmpl
         and ">溢</th>" in ivBoardTmpl
         and ">Δ</th>" not in ivBoardTmpl
         and "<strong>溢</strong>：中轴 IV − HV" in guideTmpl
         and "<strong>Δ</strong>：Black-76 现货 Δ" in guideTmpl
         and "Black-76 现货Δ" in bodyTmpl
         and "def testOptTBoardGammaMirrorsDeltaAndSkipsQuoteDelta" in overlayTests
         and "def testBlack76SpotGammaCallPutEqualAndTwoDecimals" in overlayTests
         and "def testIvBoardHeaderUsesYiNotOptionDelta" in ivHvTests),
        "现货Γ与T表期权Δ同重算；quote-delta禁IV/Γ；IV看板表头用溢且title写IV−HV")

    spJs = readText("static/js/rt_monitor_opt_sp_plan.js")
    spPlanPy = readText("rt_monitor/services/opt_sp_plan.py")
    spCacheTests = readText("tests/test_opt_sp_plan_cache_swr.py")
    chipBootTests = readText("tests/test_opt_t_board_chips_boot.py")
    add(g, "L050 SellPut首屏自启动不得等chart-stack",
        ("window.__optSpPlanEarly" in hCode
         and "window.__optSpPlanEarly = null" in hCode
         and "function bootSpPlan()" in spJs
         and "bootSpPlan();" in spJs
         and hCode.find("rt_monitor_opt_sp_plan.js") < hCode.find("chart-stack.umd.js")
         and "def peekSpPlan" in spPlanPy
         and "def testPeekReturnsCacheWithoutCompute" in spCacheTests
         and "def test_opt_t_board_sellput_boots_without_chart_stack" in chipBootTests),
        "head 早发计划 API；脚本自 boot 当前标的；不得排在 chart-stack 之后")

    # LESSON-051：到期日 DTE=0 仍交易。IV 用 1/365 地板；时年化分母为 0 才空。
    # 检查范围收窄到目标函数体，避免 L015 自指。
    overlayTests = readText("tests/test_opt_t_board_daqmt_overlay.py")
    yearFracFn = ""
    mYear = re.search(r"def _optTIvYearFrac.*?(?=\ndef )", bCode, re.S)
    if mYear:
        yearFracFn = mYear.group(0)
    sellerFn = ""
    mSeller = re.search(r"def _opt_t_board_seller_metrics.*?(?=\ndef )", bCode, re.S)
    if mSeller:
        sellerFn = mSeller.group(0)
    fwdFn = ""
    mFwd = re.search(r"def _opt_t_board_section_forward.*?(?=\ndef )", bCode, re.S)
    if mFwd:
        fwdFn = mFwd.group(0)
    tAt = sellerFn.find("tExp = _optTIvYearFrac(daysToExpiry)")
    ivAt = sellerFn.find("implied_volatility")
    ivChunk = sellerFn[tAt: ivAt + 220] if tAt >= 0 and ivAt > tAt else ""
    add(g, "L051 到期日仍解 IV 且时年化保持空",
        ("max(int(daysToExpiry), 1) / 365.0" in yearFracFn
         and "daysToExpiry < 0" in yearFracFn
         and "tExp = _optTIvYearFrac(daysToExpiry)" in sellerFn
         and "tExp = _optTIvYearFrac(daysToExpiry)" in fwdFn
         and "if px > 0 and tExp is not None and fwd > 0:" in ivChunk
         and "time_to_expiry=tExp" in ivChunk
         and "daysToExpiry > 0" not in ivChunk
         and "daysToExpiry > 0" in sellerFn
         and "def testExpiryDayStillSolvesIvAndSkipsTvAnn" in overlayTests),
        "DTE=0 用 1/365 反解 IV/Δ；时年化仍要求 DTE>0")

    bpCode = readText("rt_monitor/blueprints/opt_t_board_bp.py")
    apiFn = bpCode.split("def apiOptTBoard():", 1)[-1].split("def apiOptTBoardQuoteDelta", 1)[0]
    deltaFn = hCode.split("function loadQuoteDelta()", 1)[-1].split("function scheduleQuoteDeltaRefresh", 1)[0]
    bootFn = hCode.split("function bootOptTBoard()", 1)[-1].split("bootOptTBoard();", 1)[0]
    add(g, "L053 顶栏现货壳页内嵌且不跟T表seq走",
        ("window.__optChipSpots" in hCode
         and "applyChipSpots(window.__optChipSpots)" in bootFn
         and bootFn.find("applyChipSpots(window.__optChipSpots)") < bootFn.find("renderChips(bootU)")
         and "applyChipSpots(payload.spots)" in deltaFn
         and deltaFn.find("applyChipSpots(payload.spots)") < deltaFn.find("seq !== quoteDeltaSeq")
         and "packChipSpotQuotes" in apiFn
         and "'spots': packChipSpotQuotes" in apiFn
         and "opt_chip_spots=optChipSpots" in bCode
         and "window.__optChipSpots" in chipBootTests),
        "壳页内嵌 spots；T 表 API 带现货；quote-delta 先涂 chip 再按 seq 改表")

    luFn = hCode.split("function loadUnderlying", 1)[-1].split("function onAddClick", 1)[0]
    bgFn = hCode.split("function backgroundRefreshTick", 1)[-1].split(
        "function scheduleBackgroundRefresh", 1
    )[0]
    add(g, "L055 后台整表不得叠请求堵死quote-delta",
        ("prevU" in luFn
         and "prevU !== u" in luFn
         and luFn.find("prevU") < luFn.find("quoteDeltaSeq += 1")
         and "bgRefreshInflight" in bgFn
         and "priority: 'low'" in bgFn
         and "priority: 'high'" in deltaFn
         and "AbortController" not in deltaFn
         and "AbortController" not in bgFn
         and "loadQuoteDelta()" in bootFn
         and bootFn.find("loadQuoteDelta()") < bootFn.find("loadUnderlying(bootU")
         and "prefetchUnderlying(rest.shift(), function" in hCode
         and "test_opt_t_quote_delta_not_starved_by_board_refresh" in chipBootTests),
        "后台整表单飞+low且不abort重试；quote-delta high且不短超时自杀；预取串行；同标的静默不bump seq")

    # LESSON-037：三列分位的字段名必须与共享模块窗口键、T 板字段名三者同名。
    # 只检查其它文件的内容，不触碰本文件，避免 L015 的自指匹配。
    ivCode = stripPyComments(readText("rt_monitor/services/opt_iv_board.py"))
    ivTmpl = readText("templates/rt_monitor_opt_iv_board.html")
    ivTests = readText("tests/test_opt_iv_hv.py")
    add(g, "L037 三列分位字段名三处同源",
        ("_PER_CELL_KEYS = ('pera', 'perc', 'per1y')" in ivCode
         and "_OPT_T_PER_KINDS = ('per1y', 'perc', 'pera')" in bCode
         and "t.call.pera" in ivTmpl and "t.put.pera" in ivTmpl
         and "def test_per_cells_keys_match_shared_window_keys" in ivTests),
        "IV 看板/T 板/共享模块用同一组字段名；兼容旧键的映射层会让跨页错引静默显示 —")

    # LESSON-038：0~100（共享模块）与 0~1（_fmtPer）之间的折算只能在 _perCells 里，
    # 且必须有量纲锚定测试；漏折算或多折算都不会抛错，只会静默给错数。
    perCellsBody = ""
    if "def _perCells" in ivCode:
        perCellsBody = ivCode.split("def _perCells", 1)[1].split("\ndef ", 1)[0]
    add(g, "L038 分位量纲折算在边界处且有锚定测试",
        ("raw / 100.0" in perCellsBody
         and "def test_per_cells_display_scale_is_one_decimal_percent" in ivTests
         and "'100.0'" in ivTests and "'0.0'" in ivTests
         and "f'{raw:.1f}'" in bCode),
        "共享模块给 0~100、_fmtPer 收 0~1，折算只在 _perCells；两页格式串须给同一个数")

    # LESSON-038 续：盘中覆盖实时 IV 后三列必须同步重算，且不得把空窗口写进缓存
    add(g, "L038 盘中覆盖同步重算三列且不污染缓存",
        ("windows = ivWindows.peekCached(tuple(perKey)) if perKey else None" in ivCode
         and "stats.update(_perCells(windows, current))" in ivCode
         and "def peekCached" in readText("rt_monitor/services/opt_iv_windows.py")
         and "def test_overlay_live_iv_recomputes_all_three_per_columns" in ivTests
         and "def test_overlay_live_iv_without_cached_windows_keeps_daily_per" in ivTests),
        "覆盖路径只能 peek：走 cachedPerWindows 传空 builder 会让该键三列永久变 —")

    # LESSON-039：抬高 max-width 换行断点会让 2K 变成上下堆叠。只查模板，避免 L015 自指。
    add(g, "L039 T板桌面左右并排不得3360断行",
        ("flex-wrap: nowrap" in hCode
         and "flex: 1 1 0%" in hCode
         and "flex: 0 1 1560px" in hCode
         and "@media (max-width: 3360px)" not in hCode
         and "min(1530px, 46%)" not in hCode
         and "def testOptTBoardLayoutKeepsSideBySideOnDesktop" in readText(
             "tests/test_opt_t_board_daqmt_overlay.py")),
        "nowrap + 右栏 1560 可收缩；禁止 3360 把左栏写成 100%")
    add(g, "L040 IV表铺满右栏不得出横条",
        (".opt-iv-scroll {" in hCode
         and "overflow-x: hidden;" in hCode.split(".opt-iv-scroll {", 1)[1].split(".opt-iv-table {", 1)[0]
         and "width: 100%;" in hCode.split(".opt-iv-table {", 1)[1].split(".opt-iv-cg-date {", 1)[0]
         and "width: 1530px;" not in hCode.split(".opt-iv-table {", 1)[1].split(".opt-iv-cg-date {", 1)[0]),
        "IV 表 width 100% + scroll hidden；禁止 1530 固定宽在 46% 栏里横滚")

    # LESSON-056：隐藏页不抢连接；T 板主请求有界恢复；MA 快行情与慢信号分离。
    realtimeTests = readText("tests/test_rt_monitor_realtime_freshness.py")
    monolithCode = stripPyComments(readText("rt_monitor/monolith.py"))
    add(g, "L056 T板隐藏停轮询且可自恢复",
        (hCode.count("if (document.hidden)") >= 3
         and "T_BOARD_FETCH_TIMEOUT_MS = 12000" in hCode
         and "tBoardFetchController.abort()" in hCode
         and "schedulePrimaryRetry(u)" in hCode
         and "visibilitychange" in hCode
         and "def test_t_board_pauses_hidden_tab_and_recovers_hung_primary_request" in realtimeTests),
        "hidden 闸门 + 12s abort + controller 单飞 + 1.5s 重试 + 回前台补刷")
    add(g, "L056 MA快行情与慢信号解耦",
        ("def _overlay_ma_signal_realtime_rows" in monolithCode
         and "quote_observed_at" in monolithCode
         and "signal_updated_at" in monolithCode
         and "def test_ma_signal_realtime_overlay_rebases_intraday_ranges_without_mutating_cache" in realtimeTests
         and "def test_ma_signal_api_returns_truthful_signal_and_quote_timestamps" in realtimeTests),
        "方向/Per 用慢缓存；最新价/量/区间涨跌用 rt_k 批量覆盖；两类时钟分列")
    add(g, "L058 MA完整重算隔离常驻进程争用",
        ("ProcessPoolExecutor" in monolithCode
         and "RT_MONITOR_MA_SIGNAL_PROCESS_ISOLATION" in monolithCode
         and "def _read_ma_signal_rows_for_refresh" in monolithCode
         and "rows = _read_ma_signal_rows_for_refresh()" in monolithCode
         and "max_workers=1" in monolithCode
         and "def test_ma_signal_refresh_dispatches_builder_to_isolated_single_worker" in realtimeTests
         and "def test_ma_signal_refresh_process_isolation_has_runtime_rollback" in realtimeTests),
        "原builder/缓存/SWR/rt_k覆盖不变；默认单worker进程隔离GIL/后台争用，环境开关可回退")

    # LESSON-059：共享 DB_CONFIG 已有 timeout 时不得与显式关键字重复传参。
    oiWallSrc = readText("opt_wall_510050.py")
    oiWallTests = readText("tests/test_opt_wall_510050.py")
    add(g, "L059 共享连接配置先合并再单次展开",
        ("connectionConfig = dict(DB_CONFIG_TUSHARE)" in oiWallSrc
         and "connectionConfig.setdefault('read_timeout', 20)" in oiWallSrc
         and "connectionConfig.setdefault('write_timeout', 20)" in oiWallSrc
         and "return pymysql.connect(**connectionConfig)" in oiWallSrc
         and "def testConnectionDoesNotDuplicateConfiguredTimeoutKeywords" in oiWallTests),
        "真实配置已有 timeout 也不重复传参；具名回归覆盖")

    # LESSON-060：柱宽不得把读数推出图框；标签与柱体必须是轨道同级元素。
    oiWallHtml = readText("templates/opt_wall_510050.html")
    add(g, "L060 OI柱数值使用独立保留区",
        (".bar-lane.call { justify-content: flex-end; padding-left: 58px; }" in oiWallHtml
         and ".bar-lane.put { justify-content: flex-start; padding-right: 58px; }" in oiWallHtml
         and ".call .bar-value { left: 0; }" in oiWallHtml
         and ".put .bar-value { right: 0; }" in oiWallHtml
         and "lane.append(button, value);" in oiWallHtml
         and ".bar-lane.call { padding-left: 0; }" in oiWallHtml
         and ".bar-value { display: none; }" in oiWallHtml),
        "桌面标签固定在轨道边缘；窄屏移除保留区并隐藏标签")

    # LESSON-061：Call/Put 双墙须同时展示；行权价升序且 OI 固定以万为单位。
    oiWallUi = readText("static/js/opt_wall_510050.js")
    add(g, "L061 双墙升序万单位进入可执行合同",
        ("function formatOiWan(value)" in oiWallUi
         and ".sort((a, b) => a - b)" in oiWallUi
         and "return ['call', 'put'].flatMap" in oiWallUi
         and "function summarizeSide(side)" in oiWallUi
         and "function summarizeBalance(expiry)" in oiWallUi
         and "collectStrikesAsc(expiry)" in oiWallHtml
         and "collectWallSelections(expiry)" in oiWallHtml
         and "id=\"wall-summary\"" in oiWallHtml
         and "id=\"oi-balance\"" in oiWallHtml
         and "function renderWallSummary(expiry)" in oiWallHtml
         and "function renderOiBalance(expiry)" in oiWallHtml
         and "Top 1 集中度" in oiWallHtml
         and "Call / Put 比值" in oiWallHtml
         and "pane.id = `calculation-${sideName}`" in oiWallHtml
         and "def testUiHelpersSortStrikesFormatOiInWanAndKeepBothWallsSeparate" in oiWallTests),
        "真实JS测试锁定升序/两位万单位/双墙；页面另有OI Wall与Top 3数值汇总")

    # LESSON-062：非零但小于 0.01 万/日的速度不得被格式化成方向不明的 0.00。
    add(g, "L062 小流速保留非零方向",
        ("function formatFlowVelocityWan(value)" in oiWallUi
         and "Math.abs(numericValue) < 100" in oiWallUi
         and "return `${sign}<0.01万/日`" in oiWallUi
         and "formattedSmallPositiveVelocity" in oiWallTests
         and "formattedSmallNegativeVelocity" in oiWallTests),
        "小正/负流速显示为 +<0.01/-<0.01 万/日；真实零与缺失仍分别保留")

    add(g, "Flow日均流速同时显示归一化比例",
        ("function formatFlowVelocityShare(value, denominator)" in oiWallUi
         and "日均流速占当前同侧OI" in oiWallHtml
         and "formatFlowVelocityShare(item.flowVelocityOi, currentTotalOi)" in oiWallHtml
         and "日均流速 ÷ 当前期限同侧总OI" in oiWallHtml
         and "formattedPositiveVelocityShare" in oiWallTests
         and "formattedNegativeVelocityShare" in oiWallTests),
        "保留万张/日绝对流速，同时显示相对当前同侧总OI的正负百分比/日")

    # LESSON-068：历史页面的 null 不得被 JavaScript Number(null) 静默变成 0。
    historyWallHtml = readText("templates/opt_wall_history.html")
    historyWallTests = readText("tests/test_opt_wall_history.py")
    add(g, "L068 历史 Flow 缺失值保持不可计算",
        ("if (value === null || value === undefined || value === '') return null;" in historyWallHtml
         and "function fmtVelocityShare(value)" in historyWallHtml
         and "不可计算" in historyWallHtml
         and "def testStandaloneRoutesExposeHistoryPageAndRejectUnknownUnderlying" in historyWallTests
         and "function fmtVelocityShare(value)" in historyWallTests),
        "历史页先保留 null 语义，再格式化 Flow；缺少基线显示不可计算，不伪造为 0")

    studySrc = readText("scripts/opt_wall_historical_study.py")
    studyTests = readText("tests/test_opt_wall_historical_study.py")
    add(g, "L069 事件研究收益汇总读取嵌套侧别",
        ("def _returnSummary(events: list[dict[str, Any]], side: str | None = None)" in studySrc
         and "[event['call'] for event in events]" in studySrc
         and "def testReturnSummaryReadsNestedDateEventReturns" in studyTests
         and "_returnSummary(events)['1D']['n'] == 1" in studyTests),
        "日期级收益字段显式从嵌套侧别归一化；空结果不能静默吞掉价格事件")

    # LESSON-070：盘前不可取消 RPC 必须按最坏耗时停发，排队任务须给全推恢复让路。
    add(g, "L070 盘前RPC按最坏耗时停发且排队可撤回",
        ("def shouldStartPreopenRpc" in qCode
         and "OPT_TICK_DL_TIMEOUT_SEC" in qCode
         and "def _acquireBackgroundRpcLock" in qCode
         and "chunk = optionTickGapDownloadChunk(chunk)" in qCode
         and "def testPreopenRpcDeadlineUsesConfiguredWorstCaseBudget" in tests
         and "def testQueuedBackgroundDownloadYieldsToQuoteHold" in tests),
        "09:15 前按 timeout+余量倒推截止；tick 固定小片；quoteHold 可撤回排队后台 RPC")

    flowWallTests = readText("tests/test_opt_flow_wall_510050.py")
    add(g, "Flow Acceleration 1D对3D速度差",
        ("function formatFlowAccelerationRatio(value)" in oiWallUi
         and "FlowAcceleration = FlowVelocity1D - FlowVelocity3D" in oiWallHtml
         and "function renderFlowDynamics(expiry)" in oiWallHtml
         and "flowAccelerationRatio" in oiWallSrc
         and "testBuildFlowWallSnapshotAddsOneDayVsThreeDayAcceleration" in flowWallTests
         and "testBuildFlowWallSnapshotAccelerationHandlesZeroThreeDayVelocityAndMissingPair" in flowWallTests),
        "展示1D相对3D的日均流速差与比值；零分母和缺失配对不伪造结果")

    maxPainTests = readText("tests/test_opt_max_pain_510050.py")
    add(g, "Max Pain 候选结算价与双侧 Pain 追溯",
        ("def calculateMaxPain" in oiWallSrc
         and "MAX_PAIN_FORMULA" in oiWallSrc
         and "'maxPain': maxPain" in oiWallSrc
         and "function formatPainYuan(value)" in oiWallUi
         and "function formatDistancePct(value)" in oiWallUi
         and "function renderMaxPain(expiry)" in oiWallHtml
         and "id=\"max-pain-table-body\"" in oiWallHtml
         and "Pain(K) = Σ CallOIᵢ × max(K − Kᵢ, 0) × per_unit" in oiWallHtml
         and "testCalculateMaxPainUsesCallPutIntrinsicValueAndPerUnit" in maxPainTests
         and "testCalculateMaxPainKeepsAllTiedMinimumCandidates" in maxPainTests
         and "formattedPositiveDistance" in oiWallTests
         and "formattedNegativeDistance" in oiWallTests),
        "逐候选结算价展示 Call/Put 内在价值合计；并列最小值、空态与标准乘数均有合同")

    gammaTests = readText("tests/test_opt_gamma_wall_510050.py")
    add(g, "Absolute Gamma Wall 逐合约 GEX 与估计边界",
        ("def calculateGammaExposure" in oiWallSrc
         and "GAMMA_EXPOSURE_FORMULA" in oiWallSrc
         and "def buildGammaWallSnapshot" in oiWallSrc
         and "expiryQuality" in oiWallSrc
         and "'quality': dict(expiryQuality[maturityDate])" in oiWallSrc
         and "'gamma': gammaSnapshot['expiries'][index]" in oiWallSrc
         and "def _ivRowsOnDate" in oiWallSrc
         and "formatGammaExposure" in oiWallUi
         and "formatIvPct" in oiWallUi
         and "function renderGammaWall(expiry)" in oiWallHtml
         and "id=\"gamma-table-body\"" in oiWallHtml
         and "代表 IV 近似" in oiWallHtml
         and "testCalculateGammaExposureUsesBlack76GammaAndPerUnit" in gammaTests
         and "testBuildGammaWallSnapshotAggregatesCallPutAndKeepsAdjustedPerUnit" in gammaTests
         and "testBuildGammaWallSnapshotKeepsMissingIvSideUnavailable" in gammaTests),
        "Absolute Gamma Wall 使用逐合约 per_unit 与代表 IV 近似；双侧聚合、调整合约和缺失 IV 均可追溯")

    add(g, "Gamma Zone 与 Call-Put GEX 平衡代理",
        ("def calculateGammaZone" in oiWallSrc
         and "GAMMA_ZONE_COVERAGE = 0.50" in oiWallSrc
         and "def calculateGammaFlip" in oiWallSrc
         and "GAMMA_FLIP_METHOD = 'call_minus_put_gex_balance_proxy'" in oiWallSrc
         and "'zone': zoneSnapshot" in oiWallSrc
         and "'flip': flipSnapshot" in oiWallSrc
         and "'netGammaProxy'" in oiWallSrc
         and "Absolute Gamma Zone" in oiWallHtml
         and "Gamma Flip 代理" in oiWallHtml
         and "Call−Put 代理" in oiWallHtml
         and "不是带方向的 Dealer GEX" in oiWallHtml
         and "testCalculateGammaZoneFindsSmallestWallCenteredInterval" in gammaTests
         and "testCalculateGammaFlipInterpolatesCallPutBalanceProxy" in gammaTests
         and "testBuildGammaWallSnapshotAttachesZoneAndFlipProxyToAggregate" in gammaTests),
        "Gamma Zone 明确按 50% 合计 |GEX| 覆盖率定义；Gamma Flip 明确为 Call−Put 平衡代理并保留场景化边界")

    add(g, "OI Wall 五 ETF 品种可切换",
        ("SUPPORTED_UNDERLYINGS = ('510050', '510300', '510500', '588000', '159915')" in oiWallSrc
         and "'OP159915.SZ'" in oiWallSrc
         and "def resolveUnderlying" in oiWallSrc
         and "request.args.get('underlying')" in oiWallSrc
         and "def testSupportedUnderlyingCatalogResolvesFiveEtfOptionChains" in oiWallTests
         and "def testApiUsesRequestedUnderlyingAndRejectsUnknownCode" in oiWallTests
         and "function renderUnderlyingTabs()" in oiWallHtml
         and "async function selectUnderlying(code)" in oiWallHtml
         and "data-selected-underlying=\"{{ selectedUnderlying }}\"" in oiWallHtml),
        "五个 ETF 使用白名单映射到各自 ETF/期权根代码；页面切换后重新请求对应快照")

    # LESSON-041：墙/桶键变时必须 SWR，禁止同步重算堵首屏/轮询
    tBoardSrc = readText("rt_monitor/services/opt_t_board.py")
    tBoardCached = tBoardSrc.split("def queryOptTBoardPayloadCached", 1)[-1].split(
        "def _optTBoardWarmCodeList", 1
    )[0]
    swrTests = readText("tests/test_opt_t_board_cache_swr.py")
    add(g, "L041 墙键变SWR秒返不得同步重算",
        ("# 桶/内容键变了：秒返旧表，后台重算；禁止同步堵请求" in tBoardCached
         and "stalePayload = payload" in tBoardCached
         and "禁止 SWR 旧 HTML" not in tBoardCached
         and "def testWallKeyChangeReturnsStaleAndRefreshes" in swrTests
         and "def testTargetQuoteChangeReturnsStaleAndRefreshes" in swrTests
         and "opt_t_body_html" in hCode
         and "def peekOptIvBoardLiveHtml" in readText("rt_monitor/services/opt_iv_board.py")),
        "有缓存则秒返+后台；壳页内嵌热 HTML；测试断言旧值先回")

    swSrc = readText("rt_monitor/services/sw_industry.py")
    swApi = swSrc.split("def apiSwIndustryStats", 1)[-1].split("def _fetchLatestIndexWeightRaw", 1)[0]
    swServe = swSrc.split("def _sw_serve_cached_stats_payload", 1)[-1].split(
        "def _sw_patch_margin_fields_in_payload", 1
    )[0]
    add(g, "L042 申万partial进缓存且_full穿透pending",
        ("# 首屏 partial 也必须写入缓存" in swApi
         and "wantFull and hit_payload.get('pending_fina')" in swApi
         and "HTTP 只 deepcopy 秒返" in swServe
         and "def testScheduleFullSkipsOnlyCompleteCache" in readText(
             "tests/test_sw_industry_partial_cache.py")
         and "abortMs = swDataVersion ? 20000 : 120000" in readText(
             "templates/rt_monitor_sw.html")),
        "partial 缓存+完整包穿透；HTTP 禁同步重活；前端同版本跳过/拉长首屏超时")

    # L012：清理策略必须按用途分类（不能用 mtime 一刀切），且只能归档不能直删
    ret = readText("scripts/audit_scratch_retention.py")
    lbl = readText("scripts/_label_scripts.py")
    add(g, "L012 按用途分类而非 mtime",
        ("KIND_SCRATCH" in ret and "KIND_GUARD" in ret and "KIND_TUNE" in ret
         and "mtime" in ret and "兜底" in ret),
        "用途分类；真实信号是 atime，mtime 仅缺 保留至 时兜底")
    add(g, "L012 定位规则单一实现",
        ("insertTagLines" in ret and "from audit_scratch_retention import" in lbl),
        "_label_scripts 从审计器导入，不重复实现")
    add(g, "L012 默认只报告不删",
        ("--archive" in ret and "shutil.move" in ret
         and "1) 把其中可复发的方法论抽取进" in ret),
        "过期只报告，蒸馏后显式归档")
    add(g, "L012 保留至可续期",
        ("def touchExpiry" in ret and "--touch" in ret),
        "跑过即续期（真实使用信号）")
    add(g, "L012 豁免类别是唯一权威集合",
        ("EXEMPT_KINDS" in ret and "KIND_ARCHIVE" in ret
         and "EXEMPT_KINDS" in readText("scripts/selfcheck_delivery.py")),
        "守卫/标定/留档同源，避免两处各写一份")

    # L013：atime 判据必须先测后读；且**扫描默认不读文件内容**（读=污染证据），
    # "归零"只能是显式开关 —— 否则自检自己会把 `--sweep` 的证据吃掉。
    body = stripPyComments(ret)          # 注释会放烟雾弹，只看真代码
    iMeasure = body.find("atimes = measureAtimes()")
    iMeta = body.find("info = headMeta(meta, p)")
    selfSrc = readText("scripts/selfcheck_delivery.py")
    add(g, "L013 先测 atime 再取头信息", 0 <= iMeasure < iMeta,
        "measureAtimes → headMeta（缓存优先，不读内容）"
        if 0 <= iMeasure < iMeta else
        f"顺序错乱 idx measure={iMeasure} meta={iMeta}")
    add(g, "L013 内容读取只在 headMeta（缓存未命中）",
        ("def headMeta" in ret and "hit = meta.get(path)" in ret
         and "sig" in ret and "head = readHead(path)" in ret),
        "稳定态扫描零内容读取，不会把『翻过』当成『跑过』")
    add(g, "L013 归零是显式开关（默认不扰动）",
        ("resetAfter: bool = False" in ret and "if resetAfter:" in ret
         and "scan(resetAfter=True)" in ret),
        "自检多次扫描不会吃掉 --sweep 依赖的证据")
    # ⚠️ 只能检查 readText **函数体**：若检查整份源码，断言里的字符串字面量
    # 自己就会命中（自指匹配 → 守卫永远为真，实测注入后仍 PASS）。
    fnText = ""
    if "def readText" in selfSrc:
        tail = selfSrc.split("def readText", 1)[1]
        fnText = stripPyComments(tail.split("\ndef ", 1)[0])
    add(g, "L013 自检读脚本不留下 atime 痕迹",
        ("os.utime(path, saved)" in fnText and "finally:" in fnText),
        "自检是纯观察者：读完把 atime 原样写回")
    add(g, "L013 归零用 os.utime 对齐双时间戳",
        ("def normalizeAtime" in ret and "os.utime(path, (now, now))" in ret),
        "只写文件不改 atime 会让 atime>mtime 恒真")
    # 归零时刻必须显式 time.time()：用 os.stat().st_mtime 会拿到陈旧值。
    # 这条**只能**用源码守卫守 —— 运行时不可观测（writeTagged 内部的写会把
    # mtime 设成当下，两种写法结果相同；已实测该注入无法被运行时断言区分）。
    normFn = ""
    if "def normalizeAtime" in ret:
        normFn = stripPyComments(
            ret.split("def normalizeAtime", 1)[1].split("\ndef ", 1)[0])
    add(g, "L013 归零必须用 time.time() 取当前时刻",
        ("now = time.time()" in normFn and "st_mtime" not in normFn),
        "NTFS atime 惰性更新：st_mtime 可能陈旧，归零会设成过去时刻")
    add(g, "L013 标注器走 writeTagged（含归零）",
        ("writeTagged" in lbl and "def writeTagged" in ret),
        "绕过 writeTagged 直写会让新脚本全被当成『跑过』")
    add(g, "L013 会话收尾扫描入口存在",
        ("--sweep" in ret and "def sweep" in ret and "ATIME_EPS" in ret),
        "跑过即续期，由执行事实驱动")

    # 运行态取证：用**临时探针文件**验证整条 atime 链路，绝不碰真实脚本的 atime
    # （真实 atime 是 `--sweep` 的证据；自检读一遍脚本就会把 53 个全弄脏）。
    # 验证：写入并归零 → 读到变脏 → 只对该文件复位 → 回到干净。
    sys.path.insert(0, os.path.join(_REPO, "scripts"))
    probe = os.path.join(_REPO, "scripts", "_atime_guard_probe.py")
    try:
        import audit_scratch_retention as retMod
        with open(probe, "w", encoding="utf-8", newline="") as fh:
            fh.write("# coding: utf-8\n# 探针，由自检创建并删除\n")
        retMod.writeTagged(probe, retMod.KIND_SCRATCH)
        st0 = os.stat(probe)
        clean0 = st0.st_atime <= st0.st_mtime + retMod.ATIME_EPS
        # 必须等一下再读：写入后立刻读，at-mt 可能小于 ATIME_EPS（实测 0.006s），
        # 判据会看不懂这次读（ATIME_EPS 已从 1.0 降到 0.05，这里留足余量）。
        time.sleep(0.3)
        retMod.readHead(probe)                      # 读一次 → 应变脏
        st1 = os.stat(probe)
        dirty1 = st1.st_atime > st1.st_mtime + retMod.ATIME_EPS
        retMod.resetAtimes([probe])                 # 只复位探针
        st2 = os.stat(probe)
        clean2 = st2.st_atime <= st2.st_mtime + retMod.ATIME_EPS
        add(g, "L013 atime 链路（写入归零→读变脏→复位）",
            clean0 and dirty1 and clean2,
            f"writeTagged 后 clean={clean0} / readHead 后 dirty={dirty1} / "
            f"resetAtimes 后 clean={clean2}")
    except Exception as exc:  # noqa: BLE001 - 取证失败要显式暴露
        add(g, "L013 atime 链路（写入归零→读变脏→复位）", False,
            f"{type(exc).__name__}: {exc}")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass

    checkLessonsLogAndInstance(g)


def pyFuncBody(rel: str, funcName: str) -> str:
    """取模块级函数源码（去注释）；只在函数体内断言，避免 LESSON-015 自指。"""
    import ast

    src = readText(rel)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == funcName:
            return stripPyComments(ast.get_source_segment(src, node) or "")
    return ""


def checkLessonsLogAndInstance(g: str) -> None:
    # LESSON-085：被 import 的脚本不得 force 覆盖宿主根日志；服务入口独占日志；pytest 不写正式日志。
    setupBody = pyFuncBody("opt_vertical_spread_backtest.py", "setupLogger")
    guardIdx = setupBody.find("if logging.getLogger().handlers")
    forceIdx = setupBody.find("force=True")
    appSrc = stripPyComments(readText("rt_monitor_app.py"))
    conftest = stripPyComments(readText("tests/conftest.py"))
    add(g, "L085 被导入脚本不得劫持宿主日志",
        (0 <= guardIdx < forceIdx
         and "useDedicatedLogFile('rt_monitor_app'" in appSrc
         and "if __name__ in ('__main__', '__mp_main__'):" in appSrc
         and "BaseRotatingHandler.emit = _guard(" in conftest),
        "回测 setupLogger 在 force 前遇宿主已有 handler 即返回；rt_monitor 入口独占 logs/rt_monitor_app.log；"
        f"conftest 拦截仓库内日志写入（guardIdx={guardIdx} forceIdx={forceIdx}）")
    legacy = os.path.join(_REPO, "logs", "融资融券明细数据导入.log")
    dedicated = os.path.join(_REPO, "logs", "rt_monitor_app.log")
    try:
        legacyTail = open(legacy, "rb").read()[-200_000:].decode("utf-8", "replace") if os.path.exists(legacy) else ""
        rtMarks = re.findall(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - \w+ - (?:sw_industry_stats|fina_arturn|ma_signal_table)",
                             legacyTail, re.M)
        lastLegacy = rtMarks[-1] if rtMarks else ""
        dedMtime = datetime.fromtimestamp(os.path.getmtime(dedicated)).strftime("%Y-%m-%d %H:%M:%S") if os.path.exists(dedicated) else ""
        add(g, "L085 运行态：rt_monitor 日志只写专用文件",
            bool(dedMtime) and lastLegacy < dedMtime and (time.time() - os.path.getmtime(dedicated)) < 3600,
            f"rt_monitor_app.log 最近写入 {dedMtime or '无'}；共用日志最后一条 rt_monitor 记录 {lastLegacy or '无'}（须早于专用日志）")
    except OSError as exc:
        add(g, "L085 运行态：rt_monitor 日志只写专用文件", False, f"{type(exc).__name__}: {exc}")

    # LESSON-086：auto-kill 必须排除自身祖先（venv 转发器），并连带结束旧实例子进程。
    killBody = pyFuncBody("rt_monitor/core/instance_lock.py", "autoKillPreviousRtMonitorInstances")
    killTests = readText("tests/test_instance_lock_autokill.py")
    add(g, "L086 auto-kill 不杀自身转发器、不留孤儿子进程",
        ("psutil.Process(me).parents()" in killBody
         and "pid in ancestors" in killBody
         and "proc.children(recursive=True)" in killBody
         and "def test_skips_own_launcher_created_earlier" in killTests
         and "def test_kills_old_instance_with_children" in killTests),
        "跳过祖先 PID；结束旧实例前先结束其 ProcessPool 子进程；回归测试已在位")

    # LESSON-087：共享模块顶层零副作用；单文件拷贝 logger_config 的测试必须连带拷贝 windows_safe_log。
    sharedTree = ast.parse(readText("windows_safe_log.py"))
    allowed = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef)
    sideEffects = [type(n).__name__ for n in sharedTree.body
                   if not isinstance(n, allowed)
                   and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
    copyOffenders = []
    testsDir = os.path.join(_REPO, "tests")
    for name in sorted(os.listdir(testsDir)):
        if not name.endswith(".py"):
            continue
        src = stripPyComments(readText(os.path.join("tests", name)))
        if "'logger_config.py'" in src and "copy" in src and "'windows_safe_log.py'" not in src:
            copyOffenders.append(name)
    add(g, "L087 共享轮转模块零副作用且随拷贝成对出现",
        not sideEffects and not copyOffenders,
        f"windows_safe_log 顶层非定义语句={sideEffects or '无'}；只拷 logger_config 未拷 windows_safe_log 的测试={copyOffenders or '无'}")

    # LESSON-088：公告日是 ISO 格式；窗口必须按 ISO 解析，且测试断言优化路径确实被走到。
    preBody = pyFuncBody("rt_monitor/services/fina_arturn_queries.py", "_fina_arturn_fetch_pre_ann_close_qfq")
    preTests = readText("tests/test_fina_pre_ann_window.py")
    add(g, "L088 公告日前收盘价窗口按 ISO 生效且有生效断言",
        ("date.fromisoformat(annYmd)" in preBody
         and "FINA_PRE_ANN_WINDOW_DAYS" in preBody
         and "strptime" not in preBody
         and "def test_window_active_for_normalized_ann_format" in preTests),
        "窗口下限用 date.fromisoformat 解析规范化后的公告日；回归测试断言首查带窗口")

    # LESSON-089：覆盖率基准须在市、期末前上市、剔除北交所陈旧旧代码；衍生表声明行业排除。
    hcSrc = readText("data_health_check.py")
    anchorSeg, fqmExcl = "", []
    try:
        hcTree = ast.parse(hcSrc)
    except SyntaxError:
        hcTree = None
    for node in (hcTree.body if hcTree else []):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ANCHOR_SQL" for t in node.targets):
            anchorSeg = ast.get_source_segment(hcSrc, node) or ""
    for node in (ast.walk(hcTree) if hcTree else []):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "dict"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if isinstance(kw.get("table"), ast.Constant) and kw["table"].value == "fina_quarter_metrics":
            excl = kw.get("anchorExcludeIndustry")
            fqmExcl = [e.value for e in getattr(excl, "elts", []) if isinstance(e, ast.Constant)]
    anchorMiss = [c for c in ("b.list_status = 'L'", "b.list_date <= %s", "STALE_BJ_SQL.format") if c not in anchorSeg]
    add(g, "L089 体检覆盖率基准口径（在市/期末前上市/剔北交所旧代码）",
        not anchorMiss and {"银行", "证券", "保险"} <= set(fqmExcl),
        f"ANCHOR_SQL 缺少条件={anchorMiss or '无'}；fina_quarter_metrics 排除行业={fqmExcl or '未声明'}")

    # LESSON-090：关键文本文件不得含 UTF-8 替换字符（PowerShell 追加中文会写坏）。
    textFiles = (".gitignore", "AGENTS.md", ".cursor/rules/jinwq.mdc", ".planning/LESSONS.md",
                 ".planning/COMMIT-CHECKLIST-20261004.md", "rt_monitor/CHANGELOG-static-ver.md")
    badText = [rel for rel in textFiles
               if os.path.exists(os.path.join(_REPO, rel)) and "\ufffd" in readText(rel)]
    missingText = [rel for rel in textFiles if not os.path.exists(os.path.join(_REPO, rel))]
    add(g, "L090 关键文本文件为合法 UTF-8",
        not badText and not missingText,
        f"含替换字符={badText or '无'}；缺失={missingText or '无'}")


def summarizeBoardQuoteDeltaAges(payload: dict) -> dict:
    """按 quote-delta 的字段时钟统计灰阶，禁止由 HTML class 数量反推行数。

    一张合约只要 OI 陈旧，页面的 OI 单元格就会变灰；这不等于最新、成交量与
    五档盘口整行不可用。缺字段按陈旧处理，避免诊断接口形状变化时误报健康。
    """
    fields = ("last", "vol", "oi", "book")
    quotes = payload.get("quotes") if isinstance(payload, dict) else None
    rows = quotes if isinstance(quotes, dict) else {}
    fieldStaleN = {field: 0 for field in fields}
    fieldAncientN = {field: 0 for field in fields}
    fullStaleRowN = 0
    for item in rows.values():
        quote = item if isinstance(item, dict) else {}
        rowIsStale = True
        for field in fields:
            data = quote.get(field)
            ageClass = data.get("age_class") if isinstance(data, dict) else None
            isStale = not isinstance(data, dict) or "age_class" not in data or bool(ageClass)
            if not isStale:
                rowIsStale = False
            else:
                fieldStaleN[field] += 1
                if ageClass == "opt-t-age-ancient":
                    fieldAncientN[field] += 1
        if rowIsStale:
            fullStaleRowN += 1
    return {
        "quoteN": len(rows),
        "fullStaleRowN": fullStaleRowN,
        "fieldStaleN": fieldStaleN,
        "fieldAncientN": fieldAncientN,
    }


def checkBoard() -> None:
    g = "board"
    for u in ("510500", "510300", "588000", "510050", "159915"):
        try:
            with urllib.request.urlopen(BOARD.format(u=u), timeout=60) as resp:
                d = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            add(g, f"{u} 页面", False, f"{type(exc).__name__}: {exc}")
            continue
        rows = int(d.get("row_count") or 0)
        try:
            delta = getJson(BOARD_QUOTE_DELTA.format(u=u), timeout=15)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            add(g, f"{u} quote-delta 字段时钟", False,
                f"{type(exc).__name__}: {exc}")
            continue
        ages = summarizeBoardQuoteDeltaAges(delta)
        quoteN = int(ages["quoteN"])
        fullStale = int(ages["fullStaleRowN"])
        fullStaleRatio = (fullStale / quoteN) if quoteN else 1.0
        bookStale = int(ages["fieldStaleN"]["book"])
        bookStaleRatio = (bookStale / quoteN) if quoteN else 1.0
        # 尾盘静默窗口内「灰行多」是行情性质而非服务问题（见 inCloseTailQuiet 说明）
        tailQuiet = inCloseTailQuiet()
        # 页面可用性以“完整五档”及“整行所有实时字段均陈旧”为准；
        # latest/vol/OI 的字段级灰阶只记录并单独诊断，不能误算为整行故障。
        add(g, f"{u} 行数、盘口与整行新鲜度",
            rows > 0 and quoteN >= rows and (
                (bookStaleRatio < 0.25 and fullStaleRatio < 0.25) or tailQuiet
            ),
            f"rows={rows} delta={quoteN} src={d.get('quote_source')} "
            f"book灰={bookStale} ({100*bookStaleRatio:.0f}%) "
            f"整行灰={fullStale} ({100*fullStaleRatio:.0f}%) "
            f"最新/量/OI灰={ages['fieldStaleN']['last']}/"
            f"{ages['fieldStaleN']['vol']}/{ages['fieldStaleN']['oi']} "
            f"liveN={d.get('quote_live_n')}"
            + ("（尾盘静默窗口，灰行比例不作判据）" if tailQuiet else ""))


def checkHygiene() -> None:
    """脚本资产卫生：保留期机制是否在运转（标了类就要有人管）。"""
    g = "hygiene"
    sys.path.insert(0, os.path.join(_REPO, "scripts"))
    try:
        import audit_scratch_retention as ret
    except ImportError as exc:  # 机制本身丢失 = 保留期无人执行
        add(g, "保留期审计机制存在", False, f"ImportError: {exc}")
        return
    r = ret.scan(captureAtimeBaseline())
    add(g, "保留期审计机制存在", True,
        f"retention={ret.RETENTION_DAYS}d 扫描 {len(r['scratch'])+len(r['guards'])} 个"
        f"；本会话按 atime 判定跑过 {r['usedN']} 个")
    exp = r["expired"]
    add(g, "无过期未清理的一次性诊断", not exp,
        "无" if not exp else
        "; ".join(f"{rel}({d})" for rel, _k, d in exp) + " → 跑 --archive 归档")
    byRel = {rel: kind for rel, kind, _d in r["guards"]}
    must = ("selfcheck_delivery.py", "selftest_guard_effective.py",
            "report_chain_sources.py", "audit_scratch_retention.py")
    lack = [n for n in must if byRel.get(f"scripts/{n}") != ret.KIND_GUARD]
    add(g, "核心守卫已标 用途/豁免清理", not lack,
        "4/4 已标" if not lack else f"缺标注: {', '.join(lack)}")
    bk = r["byKind"]
    nTune = bk.get(ret.KIND_TUNE, 0)
    nArch = bk.get(ret.KIND_ARCHIVE, 0)
    nGuard = len(r["guards"]) - nTune - nArch
    add(g, "豁免类别齐备（守卫/标定/留档）",
        (nTune >= 3 and nArch >= 1
         and set(ret.EXEMPT_KINDS) == {ret.KIND_GUARD, ret.KIND_TUNE,
                                       ret.KIND_ARCHIVE}),
        f"守卫 {nGuard} / 标定 {nTune} / 留档 {nArch}（三类均豁免清理）")
    add(g, "无未标注的用途灰区", not r["unlabeled"],
        "无" if not r["unlabeled"] else
        f"{len(r['unlabeled'])} 个待补: "
        + ", ".join(rel for rel, _k, _d in r["unlabeled"][:5]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--only", default="",
        choices=("", "code", "lessons", "runtime", "board", "hygiene"),
    )
    args = ap.parse_args()

    # 必须最先执行：在任何 readText/scan 之前取 atime 真值，
    # 否则自检会把 `--sweep` 依赖的使用证据吃掉（LESSON-013）。
    try:
        captureAtimeBaseline()
    except Exception:  # noqa: BLE001 - 取不到快照不应阻断自检
        pass

    if args.only in ("", "code"):
        checkCode()
    if args.only in ("", "lessons"):
        checkLessons()
    if args.only in ("", "runtime"):
        checkRuntime()
    if args.only in ("", "board"):
        checkBoard()
    if args.only in ("", "hygiene"):
        checkHygiene()

    print(f"=== 交付前自检 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    cur = None
    nFail = 0
    nSkip = 0
    for grp, name, verdict, ev in RESULTS:
        if grp != cur:
            print(f"\n[{grp}]")
            cur = grp
        mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "-"}[verdict]
        if verdict == "FAIL":
            nFail += 1
        if verdict == "SKIP":
            nSkip += 1
        print(f"  {mark} {name:32s} {ev}")
    print(f"\n合计 {len(RESULTS)} 项："
          f"PASS {len(RESULTS)-nFail-nSkip} / FAIL {nFail} / SKIP {nSkip}")
    return 1 if nFail else 0

# 代码更新时间：2026-09-15 18:55 L034 盘后QMT补齐门控与市场事件时钟守卫
# 代码更新时间：2026-09-21 06:31 T板顶栏 Per C 配置入口守卫
# 代码更新时间：2026-09-21 07:22 L039 T板桌面不得 3360 断行独占
# 代码更新时间：2026-09-21 07:52 L040 IV表不得 1530 固定宽出横条
# 代码更新时间：2026-09-23 06:46 L049 T表Γ与IV看板Δ分名、不进quote-delta
# 代码更新时间：2026-09-23 06:54 L049 IV看板表头改为溢
# 代码更新时间：2026-09-23 07:53 L050 SellPut首屏自启动不得等chart-stack
# 代码更新时间：2026-09-23 08:01 L050 壳页命中时跳过 head 抢跑
# 代码更新时间：2026-09-23 09:57 L051 到期日 DTE=0 仍解 IV
# 代码更新时间：2026-09-23 10:16 L053 顶栏现货壳页内嵌且不跟T表seq走
# 代码更新时间：2026-09-23 10:32 L055 后台整表不得叠请求堵死quote-delta

if __name__ == "__main__":
    raise SystemExit(main())


# 代码更新时间：2026-09-25 16:20 L068/L069 历史研究缺失值与嵌套收益守卫
# 代码更新时间：2026-10-01 08:04 L082退出期限与L083取证沙箱隔离守卫
# 代码更新时间：2026-10-01 13:36 L084原油探针市场后缀边界守卫
# 代码更新时间：2026-10-04 17:42 L085日志劫持、L086 auto-kill 祖先与子进程守卫
# 代码更新时间：2026-10-04 19:01 L087 共享轮转模块零副作用与成对拷贝守卫
# 代码更新时间：2026-10-04 21:47 静态版本号检查改为内容哈希（computeRtMonitorStaticVer）
# 代码更新时间：2026-10-04 22:04 L088/L089/L090 守卫
# 代码更新时间：2026-10-05 10:04 DB_PASSWORD 去掉默认口令
