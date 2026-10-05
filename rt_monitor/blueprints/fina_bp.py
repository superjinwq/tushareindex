"""财报筛选域：/rt-monitor/fina 与 fina_arturn / 固定资产序列 API。"""
from __future__ import annotations

import gzip

from flask import Blueprint, jsonify, make_response, request

from rt_monitor.services.fina_arturn import (
    queryFinaArturnDaysTable,
    queryFinaCustomBoardResolve,
    queryFinaFixedAssetsRevenueSeries,
    queryFinaInterestMarks,
    queryFinaStockBasicAct,
    queryFinaSystemSchemeCodes,
    queryFinaTop10Floatholders,
    renderFinaPageNoCache,
    saveFinaInterestMarks,
)

finaBp = Blueprint('fina', __name__)


@finaBp.route('/rt-monitor/fina', strict_slashes=False, endpoint='rt_monitor_fina')
def rtMonitorFina():
    return renderFinaPageNoCache()


def _finaArturnDaysTableResponse(payload: dict, status: int, wire: bytes | None):
    """热路径直接吐预编码 gzip，避免每次 jsonify + 现压 31MB。"""
    if status != 200 or not wire:
        return jsonify(payload), status
    acceptEnc = (request.headers.get('Accept-Encoding') or '').lower()
    if 'gzip' in acceptEnc:
        body = wire
        encoding = 'gzip'
    else:
        try:
            body = gzip.decompress(wire)
        except Exception:
            return jsonify(payload), status
        encoding = None
    resp = make_response(body)
    resp.status_code = status
    resp.mimetype = 'application/json'
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    resp.headers['Content-Length'] = str(len(body))
    resp.headers['X-Fina-Arturn-Wire'] = '1'
    if encoding:
        resp.headers['Content-Encoding'] = encoding
        resp.headers['Vary'] = 'Accept-Encoding'
    return resp


@finaBp.route('/api/fina_arturn_days_table', methods=['GET'], strict_slashes=False)
def getFinaArturnDaysTable():
    payload, status, wire = queryFinaArturnDaysTable(request.args.get('end_date'))
    return _finaArturnDaysTableResponse(payload, status, wire)


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


@finaBp.route('/api/fina_top10_floatholders', methods=['GET'], strict_slashes=False)
def getFinaTop10Floatholders():
    payload, status = queryFinaTop10Floatholders(
        request.args.get('ts_code'),
        request.args.get('end_date'),
    )
    return jsonify(payload), status


@finaBp.route('/api/fina_system_scheme_codes', methods=['GET'], strict_slashes=False)
def getFinaSystemSchemeCodes():
    payload, status = queryFinaSystemSchemeCodes(
        request.args.get('scheme'),
        request.args.get('index_code'),
    )
    return jsonify(payload), status


@finaBp.route('/api/fina_custom_board_resolve', methods=['POST'], strict_slashes=False)
def postFinaCustomBoardResolve():
    """自定义板块：上传 txt/csv/xlsx 或 JSON codes，解析为 ts_code。"""
    codesRaw = None
    nameHint = None
    fileStorage = None
    if request.files:
        fileStorage = request.files.get('file')
        nameHint = request.form.get('name')
    if request.is_json:
        body = request.get_json(silent=True) or {}
        codesRaw = body.get('codes')
        if isinstance(codesRaw, str):
            codesRaw = [codesRaw]
        if not isinstance(codesRaw, list):
            codesRaw = None
        nameHint = body.get('name') or nameHint
    payload, status = queryFinaCustomBoardResolve(
        fileStorage=fileStorage,
        codesRaw=codesRaw,
        nameHint=nameHint,
    )
    return jsonify(payload), status


@finaBp.route('/api/fina_interest_marks', methods=['GET', 'POST'], strict_slashes=False)
def apiFinaInterestMarks():
    """财报感兴趣股票标记：落盘工程目录 fina_interest_marks.json（与浏览器无关）。"""
    if request.method == 'GET':
        payload, status = queryFinaInterestMarks()
        return jsonify(payload), status
    body = request.get_json(force=True, silent=True)
    payload, status = saveFinaInterestMarks(body if isinstance(body, dict) else None)
    return jsonify(payload), status


# 代码更新时间：2026-07-12 22:29
# 代码更新时间：2026-08-05 09:11 /api/fina_interest_marks 落盘长期标记
# 代码更新时间：2026-09-03 08:52 /api/fina_arturn_days_table 预编码 gzip 热路径
