#!/usr/bin/env python3
"""P4.1：从 monolith 删除已迁入 fina_arturn service 的处理函数与预热。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT / 'rt_monitor' / 'monolith.py'

DROP_PREFIXES = (
    'def get_fina_stock_basic_act():',
    'def get_fina_fixed_assets_revenue_series():',
    'def get_fina_arturn_days_table():',
    'def rt_monitor_fina():',
    'def _warmup_fina_arturn_cache_async():',
)


def removeTopLevelFunc(lines: list[str], prefix: str) -> list[str]:
    start = None
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            start = i
            break
    if start is None:
        raise RuntimeError(f'未找到: {prefix!r}')
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if not ln.strip():
            end += 1
            continue
        if ln[0] in ' \t':
            end += 1
            continue
        break
    return lines[:start] + lines[end:]


def main() -> None:
    lines = MONO.read_text(encoding='utf-8').splitlines(keepends=True)
    for prefix in DROP_PREFIXES:
        lines = removeTopLevelFunc(lines, prefix)
        print('dropped', prefix)
    MONO.write_text(''.join(lines), encoding='utf-8')
    print('P4.1 strip handlers OK')


if __name__ == '__main__':
    main()
