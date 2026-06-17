"""牛散持股域：页面 + 配置/明细 API。"""
from __future__ import annotations

from flask import Blueprint, jsonify, make_response, render_template, request
from logger_config import get_logger

from rt_monitor.core.config import RT_MONITOR_STATIC_VER
from rt_monitor.services.niusan import (
    _niusan_holder_cfg_load as holderCfgLoad,
    _niusan_holder_cfg_save as holderCfgSave,
    _niusan_scheme_display_meta_by_id as schemeDisplayMetaById,
    _query_niusan_floatholder_detail as queryNiusanFloatholderDetail,
    _query_niusan_floatholder_history as queryNiusanFloatholderHistory,
    _query_niusan_holdings_page_rows as queryNiusanHoldingsPageRows,
    _render_rt_monitor_niusan_no_cache as renderNiusanPageNoCache,
)

logger = get_logger(__name__)

niusanBp = Blueprint('niusan', __name__)


@niusanBp.route('/rt-monitor/niusan', strict_slashes=False, endpoint='rt_monitor_niusan')
def rtMonitorNiusan():
    return renderNiusanPageNoCache()


@niusanBp.route('/rt-monitor/niusan/config', strict_slashes=False)
def rtMonitorNiusanConfig():
    resp = make_response(render_template('rt_monitor_niusan_config.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp


@niusanBp.route('/api/niusan_holder_config', methods=['GET', 'POST'], strict_slashes=False)
def apiNiusanHolderConfig():
    from rt_monitor.services import niusan as niusanMod

    if request.method == 'GET':
        niusanMod._niusan_holder_cfg_cached = None
        return jsonify(holderCfgLoad())
    try:
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return jsonify({'ok': False, 'error': '请求体须为 JSON 对象'}), 400
        cfg = holderCfgSave(body)
        return jsonify({'ok': True, 'config': cfg})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        logger.exception('保存 niusan_holder_config 失败: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@niusanBp.route('/api/niusan_floatholder_history', methods=['GET'], strict_slashes=False)
def apiNiusanFloatholderHistory():
    tsCode = str(request.args.get('ts_code') or '').strip()
    schemeId = str(request.args.get('scheme_id') or '').strip() or None
    out = queryNiusanFloatholderHistory(tsCode, forSchemeId=schemeId)
    if not out.get('ok'):
        err = out.get('error') or ''
        if err in ('scheme_not_found', 'scheme_no_holders'):
            code = 404 if err == 'scheme_not_found' else 400
            return jsonify(out), code
        code = 400 if err == 'invalid ts_code' else 500
        return jsonify(out), code
    return jsonify(out)


@niusanBp.route('/api/niusan_floatholder_detail', methods=['GET'], strict_slashes=False)
def apiNiusanFloatholderDetail():
    tsCode = str(request.args.get('ts_code') or '').strip()
    schemeId = str(request.args.get('scheme_id') or '').strip() or None
    out = queryNiusanFloatholderDetail(tsCode, forSchemeId=schemeId)
    if not out.get('ok'):
        err = out.get('error') or ''
        if err in ('scheme_not_found', 'scheme_no_holders'):
            code = 404 if err == 'scheme_not_found' else 400
            return jsonify(out), code
        code = 400 if err in ('invalid ts_code', 'no_anchor') else 500
        return jsonify(out), code
    return jsonify(out)


@niusanBp.route('/api/niusan_scheme_holdings', methods=['GET'], strict_slashes=False)
def apiNiusanSchemeHoldings():
    schemeId = str(request.args.get('scheme_id') or '').strip()
    if not schemeId:
        return jsonify({'ok': False, 'error': 'missing scheme_id'}), 400
    payload = queryNiusanHoldingsPageRows(for_scheme_id=schemeId)
    err = payload.get('error')
    if err == 'scheme_not_found':
        return jsonify({'ok': False, 'error': err}), 404
    if err == 'scheme_no_holders':
        return jsonify({'ok': False, 'error': err}), 400
    if err:
        return jsonify({'ok': False, 'error': str(err)}), 500
    red = payload.get('report_end_date')
    redS = red.isoformat() if hasattr(red, 'isoformat') else (str(red) if red else None)
    snDisp, rjDisp = schemeDisplayMetaById(schemeId)
    return jsonify(
        {
            'ok': True,
            'scheme_id': schemeId,
            'rows': payload.get('rows') or [],
            'amt_yi_total': payload.get('amt_yi_total', '0.00'),
            'report_end_date': redS,
            'scheme_name': snDisp,
            'related_join': rjDisp,
        }
    )

# 代码更新时间：2026-06-17 16:14
