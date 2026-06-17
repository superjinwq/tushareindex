"""融资融券域：页面 + margin_daily / margin_rzmre 序列 API。"""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from logger_config import get_logger

from rt_monitor.core.page_render import renderRtMonitorPageNoCache
from rt_monitor.core.popup_cache import popupSeriesCacheGet, popupSeriesCachePut
from rt_monitor.services.margin import queryMarginDailyRecent, queryMarginRzmreIdxSeries

logger = get_logger(__name__)

marginBp = Blueprint('margin', __name__)


@marginBp.route('/rt-monitor/margin', strict_slashes=False, endpoint='rt_monitor_margin')
def rtMonitorMargin():
    return renderRtMonitorPageNoCache('rt_monitor_margin.html')


@marginBp.route('/api/margin_daily_recent', methods=['GET'], strict_slashes=False)
def getMarginDailyRecent():
    try:
        try:
            limit = int(request.args.get('limit', default='300'))
        except (TypeError, ValueError):
            limit = 300
        cacheKey = ('margin_daily_recent', int(limit))
        hit = popupSeriesCacheGet(cacheKey)
        if hit is not None:
            return jsonify(hit)
        payload = queryMarginDailyRecent(limit=limit)
        status = 200 if not payload.get('error') else 500
        if status == 200:
            popupSeriesCachePut(cacheKey, payload)
        return jsonify(payload), status
    except Exception as e:
        logger.exception("margin_daily_recent 接口异常: %s", e)
        return jsonify({'rows': [], 'error': str(e)}), 500


@marginBp.route('/api/margin_rzmre_idx_series', methods=['GET'], strict_slashes=False)
def getMarginRzmreIdxSeries():
    try:
        try:
            limit = int(request.args.get('limit', default='300'))
        except (TypeError, ValueError):
            limit = 300
        cacheKey = ('margin_rzmre_idx_series', int(limit))
        hit = popupSeriesCacheGet(cacheKey)
        if hit is not None:
            return jsonify(hit)
        payload = queryMarginRzmreIdxSeries(limit=limit)
        status = 200 if not payload.get('error') else 500
        if status == 200:
            popupSeriesCachePut(cacheKey, payload)
        return jsonify(payload), status
    except Exception as e:
        logger.exception("margin_rzmre_idx_series 接口异常: %s", e)
        return jsonify({'rows': [], 'error': str(e)}), 500

# 代码更新时间：2026-06-17 15:17
