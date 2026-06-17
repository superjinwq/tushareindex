"""外部脚本兼容层：HTTP handler 与构建版本（standalone / 旧 import 路径）。"""
from __future__ import annotations

from rt_monitor.core.config import RT_MONITOR_STATIC_VER
from rt_monitor.monolith import (
    _request_path_matches_no_store_json_api,
    api_sw_index_amt_pctile_series,
    api_sw_industry_limit_detail,
    api_sw_industry_stats,
)

__all__ = [
    'RT_MONITOR_STATIC_VER',
    '_request_path_matches_no_store_json_api',
    'api_sw_index_amt_pctile_series',
    'api_sw_industry_limit_detail',
    'api_sw_industry_stats',
]

# 代码更新时间：2026-06-17 19:56
