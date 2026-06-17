"""市场状态域：marketinfo 历史表页面 + /api/marketinfo_recent。"""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from logger_config import get_logger

from rt_monitor.core.page_render import renderRtMonitorPageNoCache
from rt_monitor.core.popup_cache import popupSeriesCacheGet, popupSeriesCachePut
from rt_monitor.services.market_status import (
    normalizeMarketinfoRecentLimit,
    queryMarketinfoRecent,
)

logger = get_logger(__name__)

marketStatusBp = Blueprint('market_status', __name__)


@marketStatusBp.route('/rt-monitor/market-status', strict_slashes=False, endpoint='rt_monitor_market_status')
def rtMonitorMarketStatus():
    return renderRtMonitorPageNoCache('rt_monitor_market_status.html')


@marketStatusBp.route('/api/marketinfo_recent', methods=['GET'], strict_slashes=False)
def getMarketinfoRecent():
    try:
        limit = normalizeMarketinfoRecentLimit(request.args.get('limit', default='300'))
        cacheKey = ('marketinfo_recent', int(limit))
        hit = popupSeriesCacheGet(cacheKey)
        if hit is not None:
            return jsonify(hit)
        payload = queryMarketinfoRecent(limit=limit)
        status = 200 if not payload.get('error') else 500
        if status == 200:
            popupSeriesCachePut(cacheKey, payload)
        return jsonify(payload), status
    except Exception as e:
        logger.exception("marketinfo_recent 接口异常: %s", e)
        return jsonify({'rows': [], 'limit': 300, 'error': str(e)}), 500

# 代码更新时间：2026-06-17 15:17
