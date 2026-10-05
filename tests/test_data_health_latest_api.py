"""市场状态页数据体检摘要：只读 JSON、按 mtime 缓存、缺文件不报 500。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rt_monitor.services import market_status as ms  # noqa: E402


def _write(path, overall: str) -> None:
    report = {
        'generatedAt': '2026-10-04 21:00:00', 'overall': overall, 'counts': {'OK': 1, 'WARN': 1},
        'tables': 2, 'elapsedSec': 1.0, 'expectedDaily': '2026-09-30', 'expectedPeriod': '2026-06-30',
        'results': [
            {'schema': 's', 'table': 'a', 'status': 'OK', 'page': 'p'},
            {'schema': 's', 'table': 'b', 'status': 'WARN', 'page': 'p', 'brief': 'lag=1'},
        ],
    }
    path.write_text(json.dumps(report), encoding='utf-8')


def test_missing_file_is_unavailable(tmp_path) -> None:
    res = ms.readDataHealthLatest(str(tmp_path / 'none.json'))
    assert res['available'] is False and res['error']


def test_returns_only_attention_items_and_refreshes_on_mtime(tmp_path) -> None:
    ms._dataHealthCache.update(mtime=None, payload=None)
    p = tmp_path / 'latest.json'
    _write(p, 'WARN')
    res = ms.readDataHealthLatest(str(p))
    assert res['overall'] == 'WARN'
    assert res['attention'] == [{'table': 's.b', 'status': 'WARN', 'page': 'p', 'brief': 'lag=1'}]
    _write(p, 'FAIL')
    os.utime(p, (os.path.getatime(p), os.path.getmtime(p) + 5))
    assert ms.readDataHealthLatest(str(p))['overall'] == 'FAIL'


def test_route_registered() -> None:
    from rt_monitor.blueprints.market_status_bp import marketStatusBp
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(marketStatusBp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert '/api/data_health_latest' in rules

# 代码更新时间：2026-10-04 21:22 新建：data_health_check 纯函数与导入期日志回归
