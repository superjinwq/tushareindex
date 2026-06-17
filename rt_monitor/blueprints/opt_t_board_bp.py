"""期权 T 型报价板域：页面 + /api/opt-t-board 片段 API。"""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from logger_config import get_logger

from rt_monitor.services.opt_t_board import (
    _opt_t_board_render_body_fragment as renderOptTBoardBodyFragment,
    _opt_t_board_resolve_for_api as resolveOptTBoardForApi,
    _query_opt_t_board_payload as queryOptTBoardPayload,
    _render_rt_monitor_opt_t_board_no_cache as renderOptTBoardPageNoCache,
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
    return jsonify(
        {
            'ok': True,
            'u': u,
            'html': html,
            'row_count': int(payload.get('row_count') or 0),
            'payload_error': payload.get('error'),
        }
    )

# 代码更新时间：2026-06-17 15:17
