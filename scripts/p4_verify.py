#!/usr/bin/env python3
"""P4 验收：fina 页面 + 三条 API（test_client）。"""
from __future__ import annotations
# 用途: 留档
# 保留至: 2026-10-20

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PATHS = [
    '/rt-monitor/fina',
    '/api/fina_arturn_days_table?end_date=20250331',
    '/api/fina_stock_basic_act?ts_code=600519.SH',
    '/api/fina_top10_floatholders?ts_code=600519.SH&end_date=20250331',
    '/api/fina_system_scheme_codes?scheme=index_weight_latest&index_code=000300.SH',
]


def runVerify() -> bool:
    from rt_monitor.app_factory import create_app

    app, _ = create_app()
    ok = True
    with app.app_context():
        rules = [str(r.rule) for r in app.url_map.iter_rules()]
        client = app.test_client()
        print('=== P4 fina ===')
        for raw in PATHS:
            path = raw.split('?', 1)[0]
            cnt = rules.count(path)
            if cnt != 1:
                print(f'  [FAIL] 路由 {path} count={cnt}')
                ok = False
        for raw in PATHS:
            rv = client.get(raw)
            code = rv.status_code
            tag = 'OK' if code == 200 else 'FAIL'
            print(f'  [{tag}] GET {raw} -> {code}')
            if code != 200:
                ok = False
            elif raw == '/rt-monitor/fina':
                if rv.headers.get('X-RT-Monitor-Build'):
                    print('  [INFO] X-RT-Monitor-Build=', rv.headers.get('X-RT-Monitor-Build'))
                else:
                    print('  [WARN] HTML 缺少 X-RT-Monitor-Build')
            elif raw.startswith('/api/fina_arturn_days_table'):
                data = rv.get_json(silent=True) or {}
                if 'rows' not in data and 'error' not in data:
                    print('  [FAIL] arturn 响应缺少 rows/error')
                    ok = False
                else:
                    rows = data.get('rows') or []
                    print('  [INFO] arturn rows=', len(rows))
                    if rows:
                        sample = rows[0]
                        for key in ('hn_qoq', 'hn_low'):
                            if key not in sample:
                                print(f'  [FAIL] arturn 行缺少 {key}')
                                ok = False
                        detail = sample.get('holders_top3_detail') or []
                        if detail and 'hold_change' not in detail[0]:
                            print('  [FAIL] holders_top3_detail 缺少 hold_change')
                            ok = False
                        if ok and rows:
                            print('  [INFO] hn_qoq/hn_low/hold_change 字段齐全')
        ep = 'fina.rt_monitor_fina'
        hasEp = ep in app.view_functions
        print(f'  [{"OK" if hasEp else "FAIL"}] endpoint {ep}')
        if not hasEp:
            ok = False
    # standalone 兼容：须能从 compat 取 handler
    try:
        import rt_monitor.compat as mon

        for name in (
            'api_sw_industry_stats',
            'api_sw_index_amt_pctile_series',
            'api_sw_industry_limit_detail',
            'RT_MONITOR_STATIC_VER',
            '_request_path_matches_no_store_json_api',
        ):
            if not hasattr(mon, name):
                print(f'  [FAIL] compat 缺少 {name}')
                ok = False
            else:
                print(f'  [OK] compat.{name}')
    except Exception as e:
        print(f'  [FAIL] import rt_monitor.compat: {e}')
        ok = False
    # standalone 应用 smoke
    try:
        from rt_monitor_sw_standalone import createSwStandaloneApp

        swApp = createSwStandaloneApp()
        with swApp.app_context():
            swClient = swApp.test_client()
            for raw in (
                '/api/sw_industry_stats?level=l1',
                '/api/sw_index_amt_pctile_series?ts_code=801010.SI&years=3',
            ):
                rv = swClient.get(raw)
                code = rv.status_code
                tag = 'OK' if code == 200 else 'FAIL'
                print(f'  [{tag}] standalone GET {raw} -> {code}')
                if code != 200:
                    ok = False
    except Exception as e:
        print(f'  [FAIL] standalone smoke: {e}')
        ok = False
    print('=== P4', 'PASS' if ok else 'FAIL', '===')
    return ok


if __name__ == '__main__':
    sys.exit(0 if runVerify() else 1)
