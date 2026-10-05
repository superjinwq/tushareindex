# coding: utf-8
"""扫描诊断脚本的保留期，报告「可清理候选」与「必须保留」。

设计要点（2026-09-11 与用户确认的保留机制）：
  1. **分类**：只看文件头声明 `用途: 一次性诊断` 的脚本；标为
     `用途: 守卫 / 标定 / 留档` 的**一律豁免**，避免误删安全网。
  2. **真实使用信号**：`atime`（本机 `fsutil behavior query disablelastaccess`
     = 2，即**启用**）：文件被 `python x.py` 执行或被读取时，
     `atime > mtime`（placement 写入后两者相等）。`保留至: YYYY-MM-DD` 由
     `--sweep` 依据该信号自动续期；`mtime` 仅在缺 `保留至` 时兜底
     （git checkout / 编辑器保存会刷新 mtime，"碰过"不等于"用过"）。
  3. **删除前蒸馏**：过期项只**报告**，并提示先抽取方法论进
     `.planning/LESSONS.md`；默认不删、不归档，需显式 `--archive`。

**关键约束（改本文件时勿破坏）**：必须在读取任何脚本内容**之前**测 atime，
否则 `readHead` 自己会把 atime 顶到 mtime 之上，让"扫描过"伪装成"跑过"。
`measureAtimes()` 就是为此存在的，扫描循环只用它返回的缓存值。

用法：
    python -X utf8 scripts/audit_scratch_retention.py            # 只报告
    python -X utf8 scripts/audit_scratch_retention.py --touch scripts/xxx.py
    python -X utf8 scripts/audit_scratch_retention.py --sweep    # 会话收尾：按 atime 批量续期
    python -X utf8 scripts/audit_scratch_retention.py --archive  # 移入 .planning/scratch-archive/
"""
from __future__ import annotations
# 用途: 守卫
# 保留至: 2026-10-20

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE = os.path.join(_REPO, ".planning", "scratch-archive")
RETENTION_DAYS = int(os.getenv("SCRATCH_RETENTION_DAYS", "30"))

PURPOSE_RE = re.compile(r"^#\s*用途[:：]\s*(?P<kind>\S+)", re.M)
EXPIRES_RE = re.compile(r"^#\s*保留至[:：]\s*(?P<day>\d{4}-\d{2}-\d{2})", re.M)

# 标注行的唯一权威定义（`scripts/_label_scripts.py` 从这里导入，避免两套规则漂移）
TAG_LINE = re.compile(r"^#\s*(?:用途|保留至)[:：].*\r?\n", re.M)
FUTURE_LINE = re.compile(r"^from __future__ import .*\r?\n", re.M)

# 分类：只有「一次性诊断」参与保留期评估
KIND_SCRATCH = "一次性诊断"
KIND_GUARD = "守卫"
KIND_TUNE = "标定"
KIND_ARCHIVE = "留档"          # 手工留档：明确声明"不受保留期管"，也不再报为未标注
# 豁免清理的类别（唯一权威集合，selfcheck 的 [hygiene] 组直接引用它）
EXEMPT_KINDS = (KIND_GUARD, KIND_TUNE, KIND_ARCHIVE)

# atime 与 mtime 的比较容差。NTFS 实测时间戳精度到毫秒、读操作会立刻推进 atime
# （sleep=0.5s 后读 → at-mt=+0.502s），所以容差只需避开浮点噪声；
# 取 1.0s 会把"标注后马上跑一次"的真实使用漏判（已实测漏判过）。
ATIME_EPS = 0.05


def measureAtimes() -> dict:
    """在**读取任何脚本内容之前**快照 atime/mtime。

    这是本机制唯一的运行态探测；“跑过”的判据是 `atime > mtime`：
    placement（写入标注）会把两者写得相等，之后只要脚本被 `python x.py`
    执行、或被任何工具读取，atime 就会越过 mtime。
    一旦先读了内容（如 `readHead`），这个信号就自毁了，所以调用顺序不能变。
    """
    out = {}
    for p in iterScripts():
        try:
            st = os.stat(p)
        except OSError:
            continue
        out[p] = {"atime": st.st_atime, "mtime": st.st_mtime}
    return out


def iterScripts():
    d = os.path.join(_REPO, "scripts")
    for name in sorted(os.listdir(d)):
        if name.endswith(".py"):
            yield os.path.join(d, name)


def readHead(path: str, lines: int = 40) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readline() for _ in range(lines))
    except OSError:
        return ""


META_CACHE = os.path.join(_REPO, ".planning", "scratch-meta.json")


def headMeta(meta: dict, path: str) -> dict:
    """取文件头的分类元数据，并保证**不留下 atime 痕迹**。

    ⚠️ **不要加 `mtime+size` 快路径**（曾加过，造成假故障）：`copy2` 还原、
    编辑器同秒改写、以及"两行标注等长的重排"都会让 mtime+size 完全不变 →
    快路径命中并返回**旧分类** → 报告把已标注的脚本报成"未标注"（实测 41 个）。
    粒度问题是结构性的，调参数解决不了。

    现在的做法：每次都读文件头（40 行，50 多个文件仅几毫秒）→ 用 **sha1**
    与缓存比对，内容未变则沿用旧分类（只刷新签名）。读之前存下 atime、
    读完**立刻写回**，所以既不误判"跑过"，也不依赖任何时间戳粒度。
    """
    try:
        st = os.stat(path)
        saved = (st.st_atime, st.st_mtime)
    except OSError:
        return {"kind": "", "expires": None}
    head = readHead(path)
    try:
        os.utime(path, saved)            # 读完立刻还原：分类读取不得污染判据
    except OSError:
        pass
    digest = hashlib.sha1(head.encode("utf-8", "replace")).hexdigest()
    hit = meta.get(path)
    if isinstance(hit, dict) and hit.get("head") == digest:
        hit["sig"] = [round(st.st_mtime, 3), st.st_size]   # 内容未变，沿用旧分类
        return hit
    kind = classify(head)
    exp = expiresAt(head, path)
    out = {"sig": [round(st.st_mtime, 3), st.st_size], "head": digest,
           "kind": kind, "expires": exp.isoformat()}
    meta[path] = out
    return out


def loadMeta() -> dict:
    try:
        with open(META_CACHE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def saveMeta(meta: dict) -> None:
    tmp = META_CACHE + ".tmp"
    try:
        os.makedirs(os.path.dirname(META_CACHE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, META_CACHE)
    except OSError:
        pass


def classify(head: str) -> str:
    m = PURPOSE_RE.search(head)
    return m.group("kind").strip() if m else ""


def expiresAt(head: str, path: str) -> date:
    m = EXPIRES_RE.search(head)
    if m:
        try:
            return datetime.strptime(m.group("day"), "%Y-%m-%d").date()
        except ValueError:
            pass
    # 兜底：mtime + 保留期（仅参考，会因 checkout/保存而偏新）
    try:
        mt = datetime.fromtimestamp(os.path.getmtime(path)).date()
    except OSError:
        mt = date.today()
    return mt + timedelta(days=RETENTION_DAYS)


def insertTagLines(src: str, kind: str, day: date | None = None) -> str:
    """幂等地重排标注行，统一插到 `from __future__` 之后（模块级注释区）。

    规则集中在这里，`_label_scripts.py` 只负责决定 kind，不重复实现定位逻辑。
    """
    day = day or date.today()
    block = f"# 用途: {kind}\n# 保留至: {day.isoformat()}\n"
    src = TAG_LINE.sub("", src)
    last = None
    for m in FUTURE_LINE.finditer(src):
        last = m
    pos = last.end() if last is not None else 0
    return src[:pos] + block + src[pos:]


def normalizeAtime(path: str) -> bool:
    """把 atime/mtime 一起归零到 mtime —— placement 与"读后"的复位基线。

    ⚠️ 不能写成 `now = os.stat(path).st_mtime; os.utime(path, (now, now))`：
    NTFS 的 atime 是**惰性更新**的，`os.stat()` 返回的 `st_mtime` 可能是陈旧值，
    于是"归零"会把 atime 设成一个旧时间戳而非当前时刻 —— 实测该写法下
    `at-mt` 仍停在 +0.501s，判据继续误判，且**两种实现看起来一样"干净"**，
    连注入验证都抓不到。正确做法：显式 `time.time()`（已修）。

    为什么必需（三次实测踩到）：
      1. 写文件只更新 mtime，atime 仍是"上次被读"的旧值 →
         `atime > mtime` 恒成立 → 52/52 全被误判成"跑过"；
      2. 扫描器自己的 `readHead` 顺序读也顶 atime →
         跑完一次报告，下一轮 28/28 全被误判成"跑过"；
      3. 标注器"先读判重、再写盘"，跨 54 个文件耗时十几秒 →
         先处理文件的 mtime 早于后处理文件的 atime → 30/30 误判。
    """
    try:
        now = time.time()
        os.utime(path, (now, now))
        return True
    except OSError:
        return False


def resetAtimes(paths=None) -> int:
    """把指定（默认全部）脚本的 atime 复位到 mtime，返回复位个数。

    `paths` 可传单个路径列表，供自检在**临时探针文件**上验证复位机制，
    从而不必把真实脚本的 atime 归零（那会吃掉 `--sweep` 要用的证据）。
    """
    n = 0
    for p in (paths if paths is not None else list(iterScripts())):
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_atime > st.st_mtime + ATIME_EPS:
            n += normalizeAtime(p)
    return n


def writeTagged(path: str, kind: str,
                expires: date | None = None) -> bool:
    """统一写入口：重排标注 → 立即复位 atime/mtime 基线。"""
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return False
    day = expires or (date.today() + timedelta(days=RETENTION_DAYS))
    src = insertTagLines(src, kind, day)
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(src)
    except OSError:
        return False
    return normalizeAtime(path)


def touchExpiry(path: str) -> bool:
    """把文件的 `保留至` 续期到 today+RETENTION_DAYS；缺失则补 `用途`+`保留至`。

    续期会重置 atime 基线（`writeTagged` 内做）—— 下次"跑过"才有干净的参照。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return False
    m = PURPOSE_RE.search(src)
    return writeTagged(path, m.group("kind") if m else KIND_SCRATCH,
                       date.today() + timedelta(days=RETENTION_DAYS))


def scan(atimes: dict | None = None, resetAfter: bool = False) -> dict:
    """扫描全部 scripts/*.py，返回分类结果（供 CLI 与交付自检共用）。

    **atime 时序契约**：
      ① `measureAtimes()` 必须在**读任何内容之前**跑（这时读到的才是真值）
      ② 随后才 `readHead()`；本函数默认**不**改动 atime（`resetAfter=False`），
         这样"读"不会毁掉别人还要用的证据
      ③ 只有明确需要自清理的调用方（报告 / `--sweep`）传 `resetAfter=True`，
         读完把基线归零，避免下一轮把"我读过"当成"跑过"

    为什么默认不归零：交付自检会多次调用本函数，若每次都归零，
    会话收尾的 `--sweep` 就再也看不到"本会话跑过哪些脚本"（实测全部报 0）。
    `atimes` 允许调用方预先测量并全程复用同一份快照。
    """
    if atimes is None:
        atimes = measureAtimes()        # ①
    meta = loadMeta()
    today = date.today()
    scratch, guards, unlabeled, expired = [], [], [], []
    rows = []          # (path, rel, kind, expires, usedSinceBaseline)
    measured = []      # (rel, used) —— 供报告与自检取证
    for p in iterScripts():
        mt = atimes.get(p, {})
        a, m = mt.get("atime"), mt.get("mtime")
        used = bool(a is not None and m is not None and a > m + ATIME_EPS)
        info = headMeta(meta, p)        # ② 命中缓存则**不读内容**（不污染 atime）
        kind = info.get("kind") or ""
        rawExp = info.get("expires")
        exp = (datetime.strptime(rawExp, "%Y-%m-%d").date() if rawExp
               else expiresAt("", p))
        rel = os.path.relpath(p, _REPO).replace("\\", "/")
        row = (rel, kind or "(未标注)", exp)
        measured.append((rel, used))
        rows.append((p, rel, kind, exp, used))
        if kind == KIND_SCRATCH:
            scratch.append(row)
            if exp < today:
                expired.append(row)
        elif kind in EXEMPT_KINDS:
            guards.append(row)
        else:
            unlabeled.append(row)
    saveMeta(meta)
    if resetAfter:
        resetAtimes()                   # ③ 仅自清理调用方
    return {"today": today, "scratch": scratch, "guards": guards,
            "unlabeled": unlabeled, "expired": expired, "rows": rows,
            "measured": measured, "usedN": sum(1 for _r, u in measured if u),
            "byKind": {k: sum(1 for _r, kk, _d in guards if kk == k)
                       for k in EXEMPT_KINDS}}


def sweep() -> int:
    """会话收尾：把「自 placement 后被用过」的一次性诊断批量续期。

    `--sweep` 与 `--touch` 的区别：`--touch` 要人记得报文件名（靠自觉），
    `--sweep` 直接读 atime 这个执行事实（靠证据）。

    时序由 `scan()` 内部保证（测→归零→读），这里只做续期。
    """
    r = scan(resetAfter=True)
    scratchRels = {rel for rel, _k, _d in r["scratch"]}
    today = date.today()
    nIn, nExp, nSkip = 0, 0, 0
    for p, rel, kind, exp, used in r["rows"]:
        if kind != KIND_SCRATCH:
            continue
        if not used:
            nSkip += 1          # 全程没跑过 → 不续期，留给保留期到期后复盘
            continue
        if touchExpiry(p):
            newDay = (today + timedelta(days=RETENTION_DAYS)).isoformat()
            tag = "过期后仍在使用→续期" if exp < today else "在期内已续期"
            print(f"  ✓ {rel:52s} {exp} → {newDay}  {tag}")
            if exp < today:
                nExp += 1
            else:
                nIn += 1
        else:
            nSkip += 1
    print(f"\n=== 会话收尾扫描 {today}：一次性诊断 {len(scratchRels)} 个，"
          f"按 atime 判定「跑过」并续期 {nIn + nExp} 个"
          f"（其中过期仍在用 {nExp} 个）/ 未跑过跳过 {nSkip} 个 ===")
    if nSkip:
        print("未跑过的脚本不会续期 —— 到期后由默认报告列出，再决定蒸馏归档。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--touch", nargs="*", default=None,
                    help="续期指定脚本（跑过就调用它）")
    ap.add_argument("--sweep", action="store_true",
                    help="会话收尾：按 atime 自动续期「跑过」的一次性诊断")
    ap.add_argument("--reset-atime", action="store_true",
                    help="一次性修复：把 atime 复位到 mtime（清除历史污染）")
    ap.add_argument("--archive", action="store_true",
                    help="把过期的一次性诊断移入 .planning/scratch-archive/")
    args = ap.parse_args()

    if args.reset_atime:
        atimes = measureAtimes()
        n = 0
        for p in iterScripts():
            mt = atimes.get(p, {})
            if mt.get("atime", 0) > mt.get("mtime", 0) + ATIME_EPS:
                n += normalizeAtime(p)
        print(f"✓ atime 基线复位 {n} 个（清除历史写入/扫描造成的污染）")
        return 0

    if args.sweep:
        return sweep()

    if args.touch:
        for rel in args.touch:
            p = rel if os.path.isabs(rel) else os.path.join(_REPO, rel)
            print(("✓ 已续期 " if touchExpiry(p) else "✗ 续期失败 ") + rel)
        return 0

    # 报告路径的时序同样由 scan() 内部保证（测→归零→读）
    r = scan(resetAfter=True)
    today = r["today"]
    scratch, guards = r["scratch"], r["guards"]
    unlabeled, expired = r["unlabeled"], r["expired"]
    print(f"=== 诊断脚本保留期审计 {today}（保留 {RETENTION_DAYS} 天）===")
    bk = r["byKind"]
    usedMap = dict(r["measured"])
    print(f"\n[豁免清理] 共 {len(guards)} 个"
          f"（守卫 {bk.get(KIND_GUARD, 0)} / 标定 {bk.get(KIND_TUNE, 0)}"
          f" / 留档 {bk.get(KIND_ARCHIVE, 0)}）")
    for rel, kind, exp in guards:
        print(f"  ✓ {rel:52s} {kind}  保留至 {exp}")
    print(f"\n[一次性诊断 · 参与保留期] {len(scratch)} 个"
          f"（已跑过 {r['usedN']} 个，atime 判据）")
    for rel, _k, exp in scratch:
        mark = "⚠ 已过期" if exp < today else "  在期内"
        ran = "跑过" if usedMap.get(rel) else "未跑"
        print(f"  {mark} {ran} {rel:52s} 保留至 {exp}")
    if unlabeled:
        print(f"\n[未标注用途 · 请补 `# 用途:`] {len(unlabeled)} 个")
        print("  （历史里程碑脚本不参与保留期，可显式标 `# 用途: 留档` 结案）")
        for rel, _k, exp in unlabeled:
            print(f"  ? {rel:52s} 参考到期 {exp}")

    if expired:
        print(f"\n⚠ {len(expired)} 个一次性诊断已过期。清理前请：")
        print("  1) 把其中可复发的方法论抽取进 .planning/LESSONS.md")
        print("  2) 再执行 `--archive` 归档（不直接删，便于复发时翻查）")
        if args.archive:
            os.makedirs(ARCHIVE, exist_ok=True)
            for rel, _k, _e in expired:
                src = os.path.join(_REPO, rel)
                dst = os.path.join(ARCHIVE, os.path.basename(rel))
                try:
                    shutil.move(src, dst)
                    print(f"  → 已归档 {rel}")
                except OSError as exc:
                    print(f"  ✗ 归档失败 {rel}: {exc}")
    else:
        print("\n✓ 无过期的一次性诊断脚本。")
    # 报告读完即归零（scan 的 resetAfter 已做）；此处再兜一次，
    # 保证连续报告不会把"我读过"当成"跑过"（实测 28/28 误报）。
    resetAtimes()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
