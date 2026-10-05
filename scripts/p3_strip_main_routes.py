#!/usr/bin/env python3
"""P3：去掉 monolith 首页 API 的 @app.route，改由 main_bp 注册。"""
from __future__ import annotations
# 用途: 留档
# 保留至: 2026-10-20

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT / 'rt_monitor' / 'monolith.py'

# (route 行前缀, 紧随其后的 def 行前缀)
STRIPS: list[tuple[str, str]] = [
    ("@app.route('/')", 'def index():'),
    ("@app.route('/rt-monitor')", 'def rt_monitor():'),
    ("@app.route('/api/rt_median')", 'def get_rt_median():'),
    ("@app.route('/api/limit_stats')", 'def api_limit_stats():'),
    ("@app.route('/api/limit_stats_detail')", 'def api_limit_stats_detail_flat():'),
    ("@app.route('/api/limit_stats/detail/lb_zt')", 'def api_limit_lb_zt_detail():'),
    (
        "@app.route('/api/limit_stats/detail/<string:detail_kind>')",
        'def api_limit_stats_detail_path(detail_kind):',
    ),
    ("@app.route('/api/limit_stats/detail')", 'def api_limit_stats_detail():'),
    ("@app.route('/api/idx_minute_diff')", 'def get_idx_minute_diff():'),
    ("@app.route('/api/idx_minute_diff_prev5')", 'def get_idx_minute_diff_prev5():'),
    ("@app.route('/api/top_moves')", 'def get_top_moves():'),
    ("@app.route('/api/etf_bias_series')", 'def get_etf_bias_series():'),
    ("@app.route('/api/etf_mcd_series')", 'def get_etf_mcd_series():'),
    ("@app.route('/api/up_num_ma5_series')", 'def get_up_num_ma5_series():'),
    ("@app.route('/api/amount_top5_pct_series')", 'def get_amount_top5_pct_series():'),
    ("@app.route('/api/etf_bias_table')", 'def get_etf_bias_table():'),
    ("@app.route('/api/etf_ma_signal_table')", 'def get_etf_ma_signal_table():'),
    (
        "@app.route('/api/ma_sr_levels', methods=['GET', 'POST'])",
        'def api_ma_sr_levels():',
    ),
    ("@app.route('/api/etf_ma_signal_series')", 'def get_etf_ma_signal_series():'),
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
    print(f'P3 strip OK, removed {removed} @app.route lines')


if __name__ == '__main__':
    main()
