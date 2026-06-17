#!/usr/bin/env python3
"""P2：提取 niusan / opt_t_board；monolith 保留 sw/index 实现函数、去掉 @app.route。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT / 'rt_monitor' / 'monolith.py'

NIUSAN_HEADER = '''"""牛散持股域：配置 JSON + top10_floatholder 查询。"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pymysql
from flask import make_response, render_template
from logger_config import get_logger

from rt_monitor.core.config import (
    DB_CONFIG,
    DB_CONFIG_TUSHARE,
    NIUSAN_HOLDER_CONFIG_PATH,
    RT_MONITOR_STATIC_VER,
)

logger = get_logger(__name__)

'''

OPT_HEADER = '''"""期权 T 型报价板域。"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from decimal import Decimal
from typing import Any

import pymysql
from flask import make_response, render_template, request
from logger_config import get_logger

from rt_monitor.core.config import DB_CONFIG_TUSHARE, RT_MONITOR_STATIC_VER

logger = get_logger(__name__)

'''


def find(prefix: str, lines: list[str], start: int = 0) -> int:
    for i in range(start, len(lines)):
        if lines[i].startswith(prefix):
            return i
    raise RuntimeError(f'not found: {prefix!r} from {start}')


def drop(lines: list[str], a: int, b: int) -> list[str]:
    return lines[:a] + lines[b:]


def drop_route_wrapper(lines: list[str], route_line: str, handler_name: str) -> list[str]:
    """删除 @app.route + def handler(): return _render...() 共约 4 行。"""
    i = find(route_line, lines)
    if lines[i + 2].strip().startswith('return _render'):
        return drop(lines, i, i + 4)
    if lines[i + 2].strip().startswith('"""') and 'return _render' in lines[i + 3]:
        return drop(lines, i, i + 5)
    raise RuntimeError(f'unexpected route wrapper at {i}: {lines[i:i+5]!r}')


def drop_api_route_decorator(lines: list[str], route_line: str, func_name: str) -> list[str]:
    """只删 @app.route 行，保留 def func_name。"""
    i = find(route_line, lines)
    if lines[i + 1].startswith(f'def {func_name}'):
        return drop(lines, i, i + 1)
    raise RuntimeError(f'expected def {func_name} after route at {i}')


def main() -> None:
    lines = MONO.read_text(encoding='utf-8').splitlines(keepends=True)

    i_niusan_const = find('_NIUSAN_CFG_LOCK', lines)
    i_app = find('app = Flask', lines)
    i_niusan_fn = find('def _niusan_holder_cfg_normalize', lines)
    i_opt_fn = find('def _opt_t_board_norm_strike', lines)
    i_rt_stat = find('# 只读 index_stat', lines)

    niusan_text = ''.join(lines[i_niusan_const:i_app] + lines[i_niusan_fn:i_opt_fn])
    niusan_text = niusan_text.replace('_NIUSAN_HOLDER_CONFIG_PATH', 'NIUSAN_HOLDER_CONFIG_PATH')
    opt_text = ''.join(lines[i_opt_fn:i_rt_stat])

    (ROOT / 'rt_monitor/services/niusan.py').write_text(
        NIUSAN_HEADER + niusan_text + '\n# 代码更新时间：2026-06-17 15:17\n',
        encoding='utf-8',
    )
    (ROOT / 'rt_monitor/services/opt_t_board.py').write_text(
        OPT_HEADER + opt_text + '\n# 代码更新时间：2026-06-17 15:17\n',
        encoding='utf-8',
    )

    out = list(lines)
    i_opt_route = find("@app.route('/rt-monitor/opt-t-board'", out)
    i_sw_section = find('# ===================== 申万行业页', out)
    for a, b in sorted(
        [
            (i_niusan_const, i_app),
            (i_niusan_fn, i_rt_stat),
            (i_opt_route, i_sw_section),
        ],
        reverse=True,
    ):
        out = drop(out, a, b)

    out = drop_route_wrapper(out, "@app.route('/rt-monitor/sw'", 'rt_monitor_sw')
    out = drop_route_wrapper(out, "@app.route('/rt-monitor/index'", 'rt_monitor_index')
    out = drop_api_route_decorator(
        out, "@app.route('/api/sw_industry_limit_detail'", 'api_sw_industry_limit_detail'
    )
    out = drop_api_route_decorator(out, "@app.route('/api/sw_industry_stats'", 'api_sw_industry_stats')
    out = drop_api_route_decorator(
        out, "@app.route('/api/index_industry_stats'", 'api_index_industry_stats'
    )

    MONO.write_text(''.join(out), encoding='utf-8')
    print('P2 extract OK')


if __name__ == '__main__':
    main()
