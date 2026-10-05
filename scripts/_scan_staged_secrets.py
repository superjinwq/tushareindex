# -*- coding: utf-8 -*-
# 用途: 守卫
# 保留至: 2026-12-31
"""扫描 git 暂存区：56 位 token、字面量 123456、智谱 key。发现即非 0 退出。"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOK = re.compile(r'\b[0-9a-f]{56}\b')
PWD = re.compile(r"""['"]123456['"]""")
ZHIPU = re.compile(r'\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b')
SK = re.compile(r'\bsk-[A-Za-z0-9]{16,}\b')


def main() -> int:
    r = subprocess.run(
        ['git', 'diff', '--cached', '--name-only', '-z'],
        cwd=ROOT, capture_output=True, check=True,
    )
    names = [n.decode('utf-8', 'replace') for n in r.stdout.split(b'\0') if n]
    bad = 0
    for rel in names:
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        hits = []
        if TOK.search(text):
            hits.append('token56')
        if PWD.search(text):
            hits.append('pwd123456')
        if ZHIPU.search(text):
            hits.append('zhipu')
        if SK.search(text):
            hits.append('sk')
        if hits:
            print('SECRET', rel, ','.join(hits))
            bad += 1
    print('staged', len(names), 'secret_files', bad)
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())

# 代码更新时间：2026-10-05 10:04
