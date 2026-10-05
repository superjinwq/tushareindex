"""自公告%基数：公告日前窗口查询 + 窗口外补查，结果须与无下限查询逐项相等，且窗口确实生效。"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.services import fina_arturn_queries as finaQ  # noqa: E402

D = dt.date
DATA = {
    'A.SH': [(D(2026, 8, 27), 10.0), (D(2026, 8, 28), 11.0)],
    'B.SZ': [(D(2025, 1, 2), 5.0)],  # 长停牌：窗口内无行，须补查到 2025 年
    'C.BJ': [(D(2026, 9, 1), 7.0)],  # 只有公告日之后的行：两轮都查不到
}


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, str | None, list[str]]] = []
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None

    def execute(self, sql: str, args: list) -> None:
        hasLo = 'trade_date >= %s' in sql
        ann = D.fromisoformat(args[0])
        lo = D.fromisoformat(args[1]) if hasLo else None
        codes = args[2:] if hasLo else args[1:]
        self.calls.append((hasLo, args[1] if hasLo else None, list(codes)))
        self._rows = []
        for c in codes:
            cand = [(d, v) for d, v in DATA.get(c, []) if d < ann and (lo is None or d >= lo)]
            if cand:
                self._rows.append((c, max(cand)[1]))

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self) -> None:
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur


def test_window_then_fallback_matches_unbounded() -> None:
    conn = FakeConn()
    pairs = [('A.SH', '2026-08-30'), ('B.SZ', '2026-08-30'), ('C.BJ', '2026-08-30')]
    out = finaQ._fina_arturn_fetch_pre_ann_close_qfq(conn, pairs)
    assert out == {('A.SH', '2026-08-30'): 11.0, ('B.SZ', '2026-08-30'): 5.0}
    first, second = conn.cur.calls
    assert first[0] is True and first[1] == '2026-07-01'
    assert second == (False, None, ['B.SZ', 'C.BJ'])


def test_window_active_for_normalized_ann_format() -> None:
    ann = finaQ._fina_arturn_normalize_ann_date_ymd('20260830')
    conn = FakeConn()
    finaQ._fina_arturn_fetch_pre_ann_close_qfq(conn, [('A.SH', ann)])
    assert [c[0] for c in conn.cur.calls] == [True]

# 代码更新时间：2026-10-04 21:42 新建：自公告% 窗口查询等价性与窗口生效回归
