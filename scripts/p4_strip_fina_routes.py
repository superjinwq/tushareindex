#!/usr/bin/env python3
"""P4：去掉 monolith 财报域 @app.route，改由 fina_bp 注册。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT / 'rt_monitor' / 'monolith.py'

STRIPS: list[tuple[str, str]] = [
    ("@app.route('/rt-monitor/fina', strict_slashes=False)", 'def rt_monitor_fina():'),
    (
        "@app.route('/api/fina_arturn_days_table', methods=['GET'], strict_slashes=False)",
        'def get_fina_arturn_days_table():',
    ),
    (
        "@app.route('/api/fina_fixed_assets_revenue_series', methods=['GET'], strict_slashes=False)",
        'def get_fina_fixed_assets_revenue_series():',
    ),
    (
        "@app.route('/api/fina_stock_basic_act', methods=['GET'], strict_slashes=False)",
        'def get_fina_stock_basic_act():',
    ),
]


def main() -> None:
    lines = MONO.read_text(encoding='utf-8').splitlines(keepends=True)
    out = list(lines)
    removed = 0
    for route_prefix, def_prefix in STRIPS:
        for i, line in enumerate(out):
            if line.startswith(route_prefix) and i + 1 < len(out) and out[i + 1].startswith(
                def_prefix
            ):
                out.pop(i)
                removed += 1
                break
        else:
            raise RuntimeError(f'未找到可剥离路由: {route_prefix!r}')
    MONO.write_text(''.join(out), encoding='utf-8')
    print(f'P4 strip OK, removed {removed} @app.route lines')


if __name__ == '__main__':
    main()
