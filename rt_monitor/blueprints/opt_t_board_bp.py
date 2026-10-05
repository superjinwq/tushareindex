"""期权 T 型报价板域：页面 + /api/opt-t-board 片段 API + IV 序列弹窗 API。"""
from __future__ import annotations

from flask import Blueprint, jsonify, make_response, request
from logger_config import get_logger

from rt_monitor.services.opt_iv_board import (
    queryOptIvBoardPayload,
    queryOptIvSeries,
    renderOptIvBoardFragment,
)
from rt_monitor.services.opt_sp_plan import (
    confirmSpFill,
    correctSpOpen,
    querySpPlan,
    querySpPreview,
)
from rt_monitor.services.opt_t_board import (
    _opt_t_board_render_body_fragment as renderOptTBoardBodyFragment,
    _opt_t_board_resolve_for_api as resolveOptTBoardForApi,
    queryOptTBoardQuoteDelta,
    queryOptTBoardIvDelta,
    queryOptTBoardPayloadCached as queryOptTBoardPayload,
    _render_rt_monitor_opt_t_board_no_cache as renderOptTBoardPageNoCache,
    optTBoardNotModified,
    optQuoteLivePollMs,
    packChipSpotQuotes,
    fetchDaqmtQuoteSnapshot,
)

logger = get_logger(__name__)

optTBoardBp = Blueprint('opt_t_board', __name__)


@optTBoardBp.route('/rt-monitor/opt-t-board', strict_slashes=False, endpoint='rt_monitor_opt_t_board')
def rtMonitorOptTBoard():
    return renderOptTBoardPageNoCache()


@optTBoardBp.route('/api/opt-t-board', methods=['GET'], strict_slashes=False)
def apiOptTBoard():
    uRaw = request.args.get('u')
    u, err = resolveOptTBoardForApi(uRaw)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    payload = queryOptTBoardPayload(u)
    html = renderOptTBoardBodyFragment(payload)
    etag = str(payload.get('_htmlEtag') or '')
    pollMs = optQuoteLivePollMs(payload.get('quote_source') or '')
    if optTBoardNotModified(request.headers.get('If-None-Match'), etag):
        resp = make_response('', 304)
        resp.headers['ETag'] = etag
        resp.headers['X-Opt-Poll-Ms'] = str(pollMs)
        resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
        return resp
    resp = jsonify(
        {
            'ok': True,
            'u': u,
            'html': html,
            'row_count': int(payload.get('row_count') or 0),
            'payload_error': payload.get('error'),
            'quote_source': payload.get('quote_source') or 'opt_daily',
            'quote_live_n': int(payload.get('quote_live_n') or 0),
            'poll_ms': pollMs,
            'trend_sma120': payload.get('trend_sma120') or {},
            'trend_sma120_by_code': payload.get('trend_sma120_by_code') or {},
            'spots': packChipSpotQuotes(fetchDaqmtQuoteSnapshot()),
        }
    )
    if etag:
        resp.headers['ETag'] = etag
    resp.headers['X-Opt-Poll-Ms'] = str(pollMs)
    return resp


@optTBoardBp.route('/api/opt-t-board/quote-delta', methods=['GET'], strict_slashes=False)
def apiOptTBoardQuoteDelta():
    """一秒级轻量字段补丁：仅读取 DAQMT 快照，不触发 SQL/IV/HTML 重算。"""
    uRaw = request.args.get('u')
    u, err = resolveOptTBoardForApi(uRaw)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    payload = queryOptTBoardQuoteDelta(u)
    resp = jsonify(payload)
    resp.headers['X-Opt-Delta-Poll-Ms'] = str(int(payload.get('poll_ms') or 1000))
    # 行情快照只能由客户端主动拉取，不能被中间缓存重放成“新数据”。
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@optTBoardBp.route('/api/opt-t-board/iv-delta', methods=['GET'], strict_slashes=False)
def apiOptTBoardIvDelta():
    """可见链 2 秒 IV/Δ/Γ/Per。不读 SQL，不改盘口补丁。"""
    uRaw = request.args.get('u')
    u, err = resolveOptTBoardForApi(uRaw)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    payload = queryOptTBoardIvDelta(u)
    resp = jsonify(payload)
    resp.headers['X-Opt-Iv-Poll-Ms'] = str(int(payload.get('poll_ms') or 2000))
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@optTBoardBp.route('/api/opt-iv-board', methods=['GET'], strict_slashes=False)
def apiOptIvBoard():
    """右侧 IV 看板 HTML 片段，与 T 型并行加载。"""
    payload = queryOptIvBoardPayload()
    html = renderOptIvBoardFragment(payload)
    etag = str(payload.get('_htmlEtag') or '')
    src = str(payload.get('quote_source') or '')
    pollMs = 2000 if src in ('daqmt', 'tdx', 'mixed') else optQuoteLivePollMs(src)
    if optTBoardNotModified(request.headers.get('If-None-Match'), etag):
        resp = make_response('', 304)
        resp.headers['ETag'] = etag
        resp.headers['X-Opt-Poll-Ms'] = str(pollMs)
        return resp
    resp = jsonify(
        {
            'ok': True,
            'html': html,
            'quote_source': payload.get('quote_source') or 'opt_iv_panel',
            'poll_ms': pollMs,
        }
    )
    if etag:
        resp.headers['ETag'] = etag
    resp.headers['X-Opt-Poll-Ms'] = str(pollMs)
    return resp


@optTBoardBp.route('/api/opt-iv-perc-anchors', methods=['GET', 'POST'], strict_slashes=False)
def apiOptIvPercAnchors():
    """Per C 锚点：GET 当前生效值；POST 写入页面 JSON 覆盖并立即失效缓存。"""
    from rt_monitor.services.opt_iv_windows import percAnchorCatalog, savePercAnchors

    if request.method == 'GET':
        resp = jsonify({'ok': True, 'items': percAnchorCatalog()})
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    body = request.get_json(silent=True) or {}
    anchors = body.get('anchors')
    if not isinstance(anchors, dict):
        return jsonify({'ok': False, 'error': '请求须含 anchors 对象'}), 400
    items, err = savePercAnchors(anchors)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    resp = jsonify({'ok': True, 'items': items})
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@optTBoardBp.route('/api/opt-iv-series', methods=['GET'], strict_slashes=False)
def apiOptIvSeries():
    """IV 弹窗：全样本 representative_iv + spot（ETF 现货）。"""
    data = queryOptIvSeries(
        request.args.get('u'),
        request.args.get('tenor'),
        request.args.get('cp') or request.args.get('call_put'),
    )
    status = 200 if data.get('ok') else 400
    return jsonify(data), status


@optTBoardBp.route('/api/opt-sp-plan', methods=['GET'], strict_slashes=False)
def apiOptSpPlan():
    uRaw = (request.args.get('u') or '').strip()
    histRaw = request.args.get('hist') or '5'
    try:
        histLimit = 80 if histRaw == 'all' else max(1, min(int(histRaw), 80))
    except ValueError:
        histLimit = 5
    data = querySpPlan(
        uRaw,
        historyLimit=histLimit,
        capitalWan=request.args.get('capitalWan'),
    )
    status = 200 if data.get('ok') else 400
    return jsonify(data), status


@optTBoardBp.route('/api/opt-sp-preview', methods=['GET'], strict_slashes=False)
def apiOptSpPreview():
    """通道/Gap 盘中预演；不写信号表、不改正式计划。"""
    data = querySpPreview(
        (request.args.get('u') or '').strip(),
        capitalWan=request.args.get('capitalWan'),
    )
    return jsonify(data), 200 if data.get('ok') else 400


@optTBoardBp.route('/api/opt-sp-plan/confirm', methods=['POST'], strict_slashes=False)
def apiOptSpPlanConfirm():
    payload = request.get_json(silent=True) or {}
    data = confirmSpFill(payload)
    status = 200 if data.get('ok') else 400
    return jsonify(data), status


@optTBoardBp.route('/api/opt-sp-plan/correct', methods=['POST'], strict_slashes=False)
def apiOptSpPlanCorrect():
    payload = request.get_json(silent=True) or {}
    data = correctSpOpen(payload)
    status = 200 if data.get('ok') else 400
    return jsonify(data), status


# 代码更新时间：2026-08-13 10:39
# 代码更新时间：2026-08-14 22:20 /api/opt-iv-board；T 型走缓存查询
# 代码更新时间：2026-08-14 23:10 /api/opt-sp-plan 支持 capitalWan
# 代码更新时间：2026-08-15 07:48 /api/opt-sp-plan/correct 更正开仓
# 代码更新时间：2026-08-24 08:03 /api/opt-iv-series 增随后 RV 与 VRP
# 代码更新时间：2026-08-24 20:38 /api/opt-t-board 返回 quote_source
# 代码更新时间：2026-08-24 21:12 /api/opt-iv-board 返回 quote_source
# 代码更新时间：2026-08-25 07:45 /api/opt-sp-preview 通道/Gap 盘中预演
# 代码更新时间：2026-08-27 14:10 /api/opt-t-board 与 IV 支持 ETag 304
# 代码更新时间：2026-08-28 06:57 返回 poll_ms / X-Opt-Poll-Ms
# 代码更新时间：2026-09-07 20:58 /api/opt-t-board 返回 trend_sma120
# 代码更新时间：2026-09-08 22:31 /api/opt-t-board 返回 trend_sma120_by_code
# 代码更新时间：2026-09-21 06:31 /api/opt-iv-perc-anchors Per C 锚点读写
# 代码更新时间：2026-09-23 10:16 /api/opt-t-board 返回 spots
