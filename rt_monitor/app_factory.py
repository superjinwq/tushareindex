"""Flask 应用工厂：P0 委托 monolith 注册路由，启动钩子集中在此。"""
from __future__ import annotations

import logging
import os
import signal
import sys

from logger_config import get_logger

from rt_monitor.core.config import REPO_ROOT, RT_MONITOR_LISTEN_PORT, RT_MONITOR_STATIC_VER
from rt_monitor.core.instance_lock import (
    ensureSingleInstanceOrExit,
    handleExitSignal,
    installWin32ConsoleCtrlHandler,
)

logger = get_logger(__name__)

_warmupStarted = False


_blueprintsRegistered = False


def create_app():
    """加载 monolith（注册全部路由）并注册已迁出的 Blueprint。"""
    global _blueprintsRegistered
    from rt_monitor.monolith import app, socketio
    if _blueprintsRegistered:
        return app, socketio

    from rt_monitor.blueprints.index_bp import indexBp
    from rt_monitor.blueprints.main_bp import mainBp
    from rt_monitor.blueprints.margin_bp import marginBp
    from rt_monitor.blueprints.market_status_bp import marketStatusBp
    from rt_monitor.blueprints.niusan_bp import niusanBp
    from rt_monitor.blueprints.opt_t_board_bp import optTBoardBp
    from rt_monitor.blueprints.sw_bp import swBp

    for bp in (mainBp, marginBp, marketStatusBp, swBp, indexBp, optTBoardBp, niusanBp):
        app.register_blueprint(bp)
    _blueprintsRegistered = True
    return app, socketio


def runStartupHooks() -> None:
    """单实例锁之后、socketio.run 之前：缓存预热与后台线程（仅调用一次）。"""
    global _warmupStarted
    if _warmupStarted:
        return
    _warmupStarted = True

    from rt_monitor.monolith import (
        _warmup_fina_arturn_cache_async,
        _warmup_ma_signal_table_cache_async,
        _warmup_sw_industry_cache_async,
        load_initial_cache,
    )

    load_initial_cache()
    try:
        import opt_spread_board_select  # noqa: F401
    except ImportError:
        logger.warning('opt_spread_board_select 不可用，opt-t-board 回测标注将退化')
    _warmup_ma_signal_table_cache_async()
    _warmup_fina_arturn_cache_async()
    _warmup_sw_industry_cache_async()


def run_server() -> None:
    """CLI 入口：单实例锁 → 预热 → socketio.run。"""
    ensureSingleInstanceOrExit(RT_MONITOR_LISTEN_PORT)
    app, socketio = create_app()
    runStartupHooks()
    try:
        signal.signal(signal.SIGINT, handleExitSignal)
        signal.signal(signal.SIGTERM, handleExitSignal)
    except Exception:
        pass
    installWin32ConsoleCtrlHandler()
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    logger.info("行情监控 Web 服务启动（rt_monitor_app.py，非 web_dashboard）")
    logger.info("访问地址: http://localhost:%s", RT_MONITOR_LISTEN_PORT)
    logger.info(
        "模板: %s  构建: %s",
        os.path.join(REPO_ROOT, 'templates', 'rt_monitor.html'),
        RT_MONITOR_STATIC_VER,
    )
    try:
        lsRoutes = sorted({str(r.rule) for r in app.url_map.iter_rules() if 'limit_stat' in str(r.rule)})
        logger.info("涨跌家数/明细相关路由: %s", lsRoutes)
        mdOk = any(str(r.rule) == '/api/margin_daily_recent' for r in app.url_map.iter_rules())
        mrzOk = any(str(r.rule) == '/api/margin_rzmre_idx_series' for r in app.url_map.iter_rules())
        logger.info(
            "融资融券接口 /api/margin_daily_recent 已注册: %s；/api/margin_rzmre_idx_series 已注册: %s  构建: %s",
            mdOk,
            mrzOk,
            RT_MONITOR_STATIC_VER,
        )
    except Exception:
        pass
    socketio.run(
        app,
        host='0.0.0.0',
        port=RT_MONITOR_LISTEN_PORT,
        debug=False,
        allow_unsafe_werkzeug=True,
    )

# 代码更新时间：2026-06-17 15:48
