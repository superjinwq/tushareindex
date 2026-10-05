# coding: utf-8
"""按归档清单把文件移回原位，并逐个校验 sha256。

用法：
    python -X utf8 scripts/restore_archived_scripts.py                 # 全部还原
    python -X utf8 scripts/restore_archived_scripts.py --dry-run       # 只检查不移动
    python -X utf8 scripts/restore_archived_scripts.py --only verify_logic.py check_smallmv_data.py
    python -X utf8 scripts/restore_archived_scripts.py --manifest .planning/scratch-archive/MANIFEST-20261004.csv

清单列：orig_path, archive_path, size, sha256, reason（路径相对 --root，默认仓库根目录）。
原位置已存在文件时拒绝覆盖；归档文件哈希与清单不符时拒绝移动。任一失败则退出码 1。
"""
from __future__ import annotations
# 用途: 留档
# 保留至: 2026-11-03

import argparse
import csv
import hashlib
import os
import shutil
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MANIFEST = os.path.join('.planning', 'scratch-archive', 'MANIFEST-20261004.csv')


def sha256Of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def loadManifest(path: str) -> list[dict]:
    with open(path, encoding='utf-8-sig', newline='') as fh:
        return list(csv.DictReader(fh))


def restoreOne(root: str, row: dict, isDryRun: bool) -> tuple[bool, str]:
    src = os.path.join(root, row['archive_path'])
    dst = os.path.join(root, row['orig_path'])
    if os.path.exists(dst):
        if os.path.exists(src):
            return False, f"原位置已有文件，拒绝覆盖：{row['orig_path']}"
        if sha256Of(dst) == row['sha256']:
            return True, f"已在原位（哈希一致），跳过：{row['orig_path']}"
        return False, f"原位置文件哈希与清单不符：{row['orig_path']}"
    if not os.path.exists(src):
        return False, f"归档文件不存在：{row['archive_path']}"
    if sha256Of(src) != row['sha256']:
        return False, f"归档文件哈希与清单不符，拒绝移动：{row['archive_path']}"
    if isDryRun:
        return True, f"[演练] 可还原：{row['archive_path']} -> {row['orig_path']}"
    os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
    shutil.move(src, dst)
    if sha256Of(dst) != row['sha256']:
        return False, f"还原后哈希不符：{row['orig_path']}"
    return True, f"已还原：{row['orig_path']}"


def main() -> int:
    ap = argparse.ArgumentParser(description='按清单还原归档脚本并校验哈希')
    ap.add_argument('--manifest', default=DEFAULT_MANIFEST)
    ap.add_argument('--root', default=_REPO, help='路径基准目录（演练时可指向临时副本）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--only', nargs='*', default=None, help='只还原这些原路径或文件名')
    args = ap.parse_args()

    manifestPath = args.manifest if os.path.isabs(args.manifest) else os.path.join(args.root, args.manifest)
    rows = loadManifest(manifestPath)
    if args.only:
        wanted = {w.replace('\\', '/') for w in args.only}
        rows = [r for r in rows if r['orig_path'] in wanted or os.path.basename(r['orig_path']) in wanted]
    okN, failN = 0, 0
    for row in rows:
        ok, msg = restoreOne(args.root, row, args.dry_run)
        print(('[成功] ' if ok else '[失败] ') + msg)
        okN += ok
        failN += not ok
    print(f'合计 {len(rows)} 项：成功 {okN} / 失败 {failN}')
    return 1 if failN else 0


if __name__ == '__main__':
    sys.exit(main())

# 代码更新时间：2026-10-04 18:55 按 MANIFEST 一键还原并校验 sha256
