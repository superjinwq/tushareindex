#!/usr/bin/env python3
"""P5.1：将 monolith 中「成交额历史极值/均值」只读查询块迁入 services/amount_extremes.py（monolith 原名 re-export）。"""
from __future__ import annotations
# 用途: 留档
# 保留至: 2026-11-04

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT / 'rt_monitor' / 'monolith.py'
OUT = ROOT / 'rt_monitor' / 'services' / 'amount_extremes.py'
FIRST = '_marketinfo_max_amount_last_n_trade_dates'
LAST = '_index_daily_mean_amount_last_n_trade_dates'

HEADER = '''"""成交额历史极值 / 区间均值只读查询（marketinfo、index_daily；P5.1 自 monolith 迁出）。"""
from __future__ import annotations

from logger_config import get_logger
from rt_monitor.core.numbers import toFloatOrNone

logger = get_logger(__name__)

_to_float_or_none = toFloatOrNone


'''


def main() -> None:
    src = MONO.read_bytes().decode('utf-8')
    nl = '\r\n' if '\r\n' in src else '\n'
    if nl == '\r\n' and src.replace('\r\n', '').count('\n'):
        raise SystemExit('monolith 换行混用 CRLF/LF，拒绝迁移')
    if OUT.exists():
        raise SystemExit(f'{OUT.name} 已存在，拒绝覆盖')
    tree = ast.parse(src)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    iFirst = next(i for i, n in enumerate(funcs) if n.name == FIRST)
    iLast = next(i for i, n in enumerate(funcs) if n.name == LAST)
    block = funcs[iFirst:iLast + 1]
    topLevel = [n for n in tree.body if block[0].lineno <= n.lineno <= block[-1].end_lineno]
    if any(not isinstance(n, ast.FunctionDef) for n in topLevel):
        raise SystemExit('区间内含非函数顶层语句，拒绝迁移')
    lines = src.split(nl)
    startIdx = block[0].lineno - 1
    while startIdx > 0 and lines[startIdx - 1].startswith('@'):
        startIdx -= 1
    endIdx = block[-1].end_lineno
    body = nl.join(lines[startIdx:endIdx]) + nl
    names = [n.name for n in block]
    OUT.write_bytes((HEADER.replace('\n', nl) + body).encode('utf-8'))
    importBlock = ['# --- 成交额历史极值/均值：services/amount_extremes（P5.1）---',
                   'from rt_monitor.services.amount_extremes import (  # noqa: E402'] + \
                  [f'    {n},' for n in names] + [')']
    newLines = lines[:startIdx] + importBlock + lines[endIdx:]
    MONO.write_bytes(nl.join(newLines).encode('utf-8'))
    print(f'extracted {len(names)} defs, {endIdx - startIdx} lines -> {OUT.name}: {names}')


if __name__ == '__main__':
    main()

# 代码更新时间：2026-10-04 21:54 新建：P5.1 抽取工具
