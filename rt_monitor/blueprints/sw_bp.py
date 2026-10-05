"""申万行业域：页面 + sw_industry_stats / sw_industry_limit_detail API。"""
from __future__ import annotations

from flask import Blueprint

from rt_monitor.core.page_render import renderRtMonitorPageNoCache

swBp = Blueprint('sw', __name__)


@swBp.route('/rt-monitor/sw', strict_slashes=False, endpoint='rt_monitor_sw')
def rtMonitorSw():
    return renderRtMonitorPageNoCache('rt_monitor_sw.html')


@swBp.route('/api/sw_industry_limit_detail', methods=['GET'], strict_slashes=False)
def apiSwIndustryLimitDetail():
    from rt_monitor.services.sw_industry import apiSwIndustryLimitDetail as handler

    return handler()


@swBp.route('/api/sw_industry_pct_t_detail', methods=['GET'], strict_slashes=False)
def apiSwIndustryPctTDetail():
    from rt_monitor.services.sw_industry import apiSwIndustryPctTDetail as handler

    return handler()


@swBp.route('/api/sw_industry_stats', methods=['GET'], strict_slashes=False)
def apiSwIndustryStats():
    from rt_monitor.services.sw_industry import apiSwIndustryStats as handler

    return handler()


@swBp.route('/api/sw_index_amt_pctile_series', methods=['GET'], strict_slashes=False)
def apiSwIndexAmtPctileSeries():
    from rt_monitor.services.sw_industry import apiSwIndexAmtPctileSeries as handler

    return handler()

# 代码更新时间：2026-08-06 10:15
