"""财报筛选域：/rt-monitor/fina 与 fina_arturn / 固定资产序列 API。"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from rt_monitor.services.fina_arturn import (
    queryFinaArturnDaysTable,
    queryFinaFixedAssetsRevenueSeries,
    queryFinaStockBasicAct,
    renderFinaPageNoCache,
)

finaBp = Blueprint('fina', __name__)


@finaBp.route('/rt-monitor/fina', strict_slashes=False, endpoint='rt_monitor_fina')
def rtMonitorFina():
    return renderFinaPageNoCache()


@finaBp.route('/api/fina_arturn_days_table', methods=['GET'], strict_slashes=False)
def getFinaArturnDaysTable():
    payload, status = queryFinaArturnDaysTable(request.args.get('end_date'))
    return jsonify(payload), status


@finaBp.route('/api/fina_fixed_assets_revenue_series', methods=['GET'], strict_slashes=False)
def getFinaFixedAssetsRevenueSeries():
    payload, status = queryFinaFixedAssetsRevenueSeries(
        request.args.get('ts_code'),
        request.args.get('end_date'),
        request.args.get('limit'),
    )
    return jsonify(payload), status


@finaBp.route('/api/fina_stock_basic_act', methods=['GET'], strict_slashes=False)
def getFinaStockBasicAct():
    payload, status = queryFinaStockBasicAct(request.args.get('ts_code'))
    return jsonify(payload), status

# 代码更新时间：2026-06-17 16:35
