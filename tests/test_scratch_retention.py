# coding: utf-8
"""保留期机制：用途分类、atime 判据、以及"读取不得污染证据"。

背景（2026-09-11）：保留期靠 `atime > mtime` 判定"这个脚本跑过"。
该判据极易被观察动作本身污染（LESSON-013），本文件把关键性质固化为回归：
  1. 只有「一次性诊断」参与保留期，守卫/标定/留档一律豁免；
  2. `writeTagged()` 写入后必须把 atime/mtime 归零（否则 atime>mtime 恒真）；
  3. `scan()` 默认**不读文件内容**（走缓存），否则"翻过"会被当成"跑过"；
  4. 归零只在显式 `resetAfter=True` 时发生（否则 `--sweep` 看不到证据）。
"""
from __future__ import annotations

import importlib.util
import os
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _loadMod():
    path = _REPO / "scripts" / "audit_scratch_retention.py"
    spec = importlib.util.spec_from_file_location("scratch_retention_mod", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class ScratchRetentionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _loadMod()

    def testExemptKindsCoverGuardTuneArchive(self):
        """豁免集合必须同时含 守卫/标定/留档，且一次性诊断**不在**其中。"""
        m = self.m
        self.assertEqual(set(m.EXEMPT_KINDS),
                         {m.KIND_GUARD, m.KIND_TUNE, m.KIND_ARCHIVE})
        self.assertNotIn(m.KIND_SCRATCH, m.EXEMPT_KINDS)

    def testWriteTaggedResetsAtimeBaseline(self):
        """写入标注后 atime 必须≈现在、且与 mtime 对齐。

        ⚠️ **可观测性边界（别浪费时间重试）**：`writeTagged` 内部的"写"会把
        mtime 设成当下，因此"用陈旧 `os.stat().st_mtime` 归零"与"用 `time.time()`
        归零"在本机**结果相同、无法用外部断言区分**（已实测：对该写法做注入，
        本测试与其它测试都不会失败）。那条边界由源码级守卫守：
        `selfcheck_delivery.py` 的 `L013 归零必须用 time.time()`。
        """
        m = self.m
        probe = _REPO / "scripts" / "_retention_probe.py"
        try:
            probe.write_text("# coding: utf-8\n", encoding="utf-8")
            self.assertTrue(m.writeTagged(str(probe), m.KIND_SCRATCH))
            st0 = os.stat(probe)
            self.assertLessEqual(
                abs(st0.st_atime - time.time()), 2.0,
                "writeTagged 后 atime 应≈现在")
            self.assertLessEqual(
                abs(st0.st_atime - st0.st_mtime), m.ATIME_EPS,
                "writeTagged 后 atime 应与 mtime 对齐")

            # 先造成脏状态（atime 远晚于 mtime 不行，这里造成 atime/mtime 都陈旧）
            past = time.time() - 600.0
            os.utime(probe, (past, past))
            self.assertTrue(m.writeTagged(str(probe), m.KIND_SCRATCH))
            st1 = os.stat(probe)
            self.assertLessEqual(
                abs(st1.st_atime - time.time()), 2.0,
                f"第二次写入后 atime 仍须≈现在，实测偏离 "
                f"{time.time() - st1.st_atime:.1f}s")
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    def testResetAtimesRestoresBaseline(self):
        """resetAtimes 能把脏文件复位；且只影响传进去的路径。"""
        m = self.m
        probe = _REPO / "scripts" / "_retention_probe2.py"
        try:
            probe.write_text("# coding: utf-8\n", encoding="utf-8")
            m.writeTagged(str(probe), m.KIND_SCRATCH)
            time.sleep(0.2)
            with open(probe, encoding="utf-8") as fh:
                fh.read()
            self.assertEqual(m.resetAtimes([str(probe)]), 1)
            st = os.stat(probe)
            self.assertLessEqual(st.st_atime, st.st_mtime + m.ATIME_EPS)
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    def testScanDoesNotReadContent(self):
        """scan() 默认不得读文件内容 —— 读=把"翻过"记成"跑过"。

        取证方式：预先放一个**已脏**的 scratch 探针并写入缓存，
        然后跑 scan(resetAfter=False)，断言该文件的 atime **没有前进**更多。
        """
        m = self.m
        probe = _REPO / "scripts" / "_retention_probe3.py"
        try:
            probe.write_text("# 用途: 一次性诊断\n# 保留至: 2099-01-01\n",
                             encoding="utf-8")
            m.writeTagged(str(probe), m.KIND_SCRATCH)
            m.scan(resetAfter=True)          # 预热缓存（此时会读一次）
            before = os.stat(probe).st_atime
            time.sleep(0.3)
            r = m.scan(resetAfter=False)     # 缓存已热，不应再读
            after = os.stat(probe).st_atime
            self.assertEqual(before, after,
                             "scan() 命中缓存时不得读内容（不得推进 atime）")
            self.assertIn("usedN", r)
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    def testMetaCacheInvalidatesOnChange(self):
        """元数据缓存以**文件头内容 sha1** 为准，文件一改必须失效。

        回归（2026-09-11 实测假故障）：曾用 `mtime+size` 做缓存**快路径**，
        于是 `copy2` 还原 / 编辑器同秒改写 / "两行标注等长的重排"都会让签名不变 →
        返回旧分类 → 报告把已标注脚本报成"未标注"（实测 41 个）。
        这里专门覆盖"mtime 与 size 都不变、只有内容变"的情形。
        """
        m = self.m
        probe = _REPO / "scripts" / "_retention_probe4.py"
        try:
            probe.write_text("# 用途: 守卫\n", encoding="utf-8")
            st0 = os.stat(probe)
            meta = {}
            self.assertEqual(m.headMeta(meta, str(probe)).get("kind"), m.KIND_GUARD)
            # 写等长内容（size 不变），并把 mtime 还原（copy2 语义）
            probe.write_text("# 用途: 标定\n", encoding="utf-8")
            os.utime(probe, (st0.st_atime, st0.st_mtime))
            st1 = os.stat(probe)
            self.assertEqual([round(st1.st_mtime, 3), st1.st_size],
                             [round(st0.st_mtime, 3), st0.st_size],
                             "前置条件：mtime 与 size 都必须未变")
            self.assertEqual(
                m.headMeta(meta, str(probe)).get("kind"), m.KIND_TUNE,
                "内容变了就必须重新分类（不能靠 mtime+size 快路径）")
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    def testHeadMetaDoesNotAdvanceAtime(self):
        """headMeta 读文件头后必须把 atime 写回 —— 分类读取不得污染"跑过"判据。"""
        m = self.m
        probe = _REPO / "scripts" / "_retention_probe5.py"
        try:
            probe.write_text("# 用途: 一次性诊断\n", encoding="utf-8")
            m.writeTagged(str(probe), m.KIND_SCRATCH)
            before = os.stat(probe).st_atime
            time.sleep(0.3)
            m.headMeta({}, str(probe))          # 冷缓存 → 会真的读一次
            after = os.stat(probe).st_atime
            self.assertEqual(before, after,
                             "headMeta 读完后必须还原 atime")
        finally:
            try:
                probe.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
