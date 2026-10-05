#!/usr/bin/env python3
"""P3 / 总验收：路由唯一性 + 关键 API smoke（无需常驻服务时用 test_client）。"""
from __future__ import annotations
# 用途: 留档
# 保留至: 2026-10-20

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STEPS: dict[str, list[str]] = {
    '3.1': ['/', '/rt-monitor', '/api/rt_median'],
    '3.2': [
        '/api/limit_stats',
        '/api/limit_stats_detail?kind=zt_t1&limit=5',
        '/api/limit_stats/detail/lb_zt?limit=5',
    ],
    '3.3': [
        '/api/etf_bias_table',
        '/api/etf_ma_signal_table',
        '/api/etf_bias_series?ts_code=510300.SH&freq=D&column=10MA&limit=10',
    ],
    '3.4': [
        '/api/idx_minute_diff',
        '/api/top_moves?limit=5',
        '/api/up_num_ma5_series?limit=5',
        '/api/amount_top5_pct_series?limit=5',
        '/api/ma_sr_levels',
        '/api/etf_ma_signal_series?ts_code=510300.SH&freq=D&limit=10',
    ],
    'P1': [
        '/api/margin_daily_recent?limit=3',
        '/api/marketinfo_recent?limit=3',
        '/rt-monitor/margin',
        '/rt-monitor/market-status',
    ],
    'P2': [
        '/rt-monitor/sw',
        '/rt-monitor/index',
        '/rt-monitor/opt-t-board',
        '/rt-monitor/niusan',
        '/api/sw_industry_stats?level=l1',
        '/api/sw_index_amt_pctile_series?ts_code=801010.SI&years=3',
        '/api/index_industry_stats',
    ],
    'P4-smoke': [
        '/rt-monitor/fina',
        '/api/fina_arturn_days_table?end_date=20250331',
        '/api/fina_top10_floatholders?ts_code=600519.SH&end_date=20250331',
        '/api/fina_system_scheme_codes?scheme=index_weight_latest&index_code=000300.SH',
    ],
}


def runStep(step: str, app=None) -> bool:
    if app is None:
        from rt_monitor.app_factory import create_app

        app, _ = create_app()
    paths = STEPS[step]
    ok = True
    with app.app_context():
        client = app.test_client()
        rules = [str(r.rule) for r in app.url_map.iter_rules()]
        print(f'\n=== {step} ===')
        for raw in paths:
            path = raw.split('?', 1)[0]
            cnt = rules.count(path)
            if cnt != 1:
                print(f'  [FAIL] 路由 {path} count={cnt}')
                ok = False
        for raw in paths:
            rv = client.get(raw)
            code = rv.status_code
            tag = 'OK' if code == 200 else 'FAIL'
            print(f'  [{tag}] GET {raw} -> {code}')
            if code != 200:
                ok = False
            elif raw in ('/', '/rt-monitor'):
                if rv.headers.get('X-RT-Monitor-Build'):
                    print('  [INFO] HTML X-RT-Monitor-Build=', rv.headers.get('X-RT-Monitor-Build'))
                else:
                    print('  [WARN] HTML 缺少 X-RT-Monitor-Build')
            elif raw == '/api/limit_stats':
                data = rv.get_json(silent=True) or {}
                if 'zt_t5' not in data:
                    print('  [FAIL] limit_stats 缺少 zt_t5')
                    ok = False
                else:
                    print('  [INFO] limit_stats keys ok, X-Limit-Stats-Panel=', rv.headers.get('X-Limit-Stats-Panel'))
            elif raw.startswith('/api/rt_median'):
                data = rv.get_json(silent=True) or {}
                if not isinstance(data.get('table'), list):
                    print('  [WARN] rt_median table 非 list（可能表空）')
        return ok


def runAll() -> bool:
    from rt_monitor.app_factory import create_app

    app, _ = create_app()
    ok = True
    for step in ('3.1', '3.2', '3.3', '3.4', 'P1', 'P2', 'P4-smoke'):
        if not runStep(step, app=app):
            ok = False
    print('\n=== 总验收', 'PASS' if ok else 'FAIL', '===')
    return ok


def main() -> None:
    step = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if step == 'all':
        sys.exit(0 if runAll() else 1)
    if step not in STEPS:
        print('用法: python scripts/p3_verify.py [3.1|3.2|3.3|3.4|P1|P2|P4-smoke|all]')
        sys.exit(2)
    sys.exit(0 if runStep(step) else 1)


if __name__ == '__main__':
    main()
