"""restore_archived_scripts：按清单还原并校验哈希；哈希不符、原位已有文件时拒绝。"""
from __future__ import annotations

import csv
import hashlib
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'restore_archived_scripts.py'


def _setup(tmp_path: Path, content: bytes = b'print(1)\n') -> Path:
    arch = tmp_path / '.planning' / 'scratch-archive'
    arch.mkdir(parents=True)
    (arch / 'a.py').write_bytes(content)
    with open(arch / 'MANIFEST-20261004.csv', 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['orig_path', 'archive_path', 'size', 'sha256', 'reason'])
        w.writeheader()
        w.writerow({'orig_path': 'a.py', 'archive_path': '.planning/scratch-archive/a.py', 'size': len(content),
                    'sha256': hashlib.sha256(content).hexdigest(), 'reason': 't'})
    return arch


def _run(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), '--root', str(tmp_path), *extra],
                          capture_output=True, text=True, encoding='utf-8', timeout=60)


def test_restore_moves_back_with_same_hash(tmp_path) -> None:
    _setup(tmp_path)
    assert _run(tmp_path, '--dry-run').returncode == 0
    assert not (tmp_path / 'a.py').exists()
    assert _run(tmp_path).returncode == 0
    assert (tmp_path / 'a.py').read_bytes() == b'print(1)\n'
    assert not (tmp_path / '.planning' / 'scratch-archive' / 'a.py').exists()


def test_restore_refuses_tampered_archive(tmp_path) -> None:
    arch = _setup(tmp_path)
    (arch / 'a.py').write_bytes(b'tampered\n')
    r = _run(tmp_path)
    assert r.returncode == 1
    assert not (tmp_path / 'a.py').exists()


def test_restore_refuses_overwrite(tmp_path) -> None:
    _setup(tmp_path)
    (tmp_path / 'a.py').write_bytes(b'newer work\n')
    r = _run(tmp_path)
    assert r.returncode == 1
    assert (tmp_path / 'a.py').read_bytes() == b'newer work\n'

# 代码更新时间：2026-10-04 18:55 还原脚本哈希校验与拒绝覆盖回归
