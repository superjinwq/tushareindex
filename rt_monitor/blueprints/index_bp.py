"""宽基指数行业域：页面 + index_industry_stats API。"""
from __future__ import annotations

from flask import Blueprint, make_response, render_template

from rt_monitor.core.config import RT_MONITOR_STATIC_VER

indexBp = Blueprint('index', __name__)


@indexBp.route('/rt-monitor/index', strict_slashes=False, endpoint='rt_monitor_index')
def rtMonitorIndex():
    from rt_monitor.services.sw_industry import INDEX_MONITOR_CODES

    resp = make_response(
        render_template(
            'rt_monitor_sw.html',
            sw_page_mode='index',
            sw_api_path='/api/index_industry_stats',
            sw_page_title='宽基指数行业分析',
            sw_show_scheme_config=False,
            sw_index_options=[
                {'index_code': code, 'index_name': name} for code, name in INDEX_MONITOR_CODES
            ],
        )
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


@indexBp.route('/api/index_industry_stats', methods=['GET'], strict_slashes=False)
def apiIndexIndustryStats():
    from rt_monitor.services.sw_industry import apiIndexIndustryStats as handler

    return handler()

# 代码更新时间：2026-06-18 06:24
