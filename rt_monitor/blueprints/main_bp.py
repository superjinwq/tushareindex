"""首页监控域：/、/rt-monitor 与 limit_stats / ETF / 指数分钟等 API。"""
from __future__ import annotations

from flask import Blueprint

mainBp = Blueprint('main', __name__)


def _mono(name: str):
    import rt_monitor.monolith as m

    return getattr(m, name)


# --- 3.1 页面 + rt_median ---
@mainBp.route('/')
def index():
    return _mono('_render_rt_monitor_no_cache')()


@mainBp.route('/rt-monitor')
def rtMonitor():
    return _mono('_render_rt_monitor_no_cache')()


@mainBp.route('/api/rt_median')
def getRtMedian():
    return _mono('get_rt_median')()


# --- 3.2 limit_stats 簇 ---
@mainBp.route('/api/limit_stats')
def apiLimitStats():
    return _mono('api_limit_stats')()


@mainBp.route('/api/limit_stats_detail')
def apiLimitStatsDetailFlat():
    return _mono('api_limit_stats_detail_flat')()


@mainBp.route('/api/limit_stats/detail/lb_zt')
def apiLimitLbZtDetail():
    return _mono('api_limit_lb_zt_detail')()


@mainBp.route('/api/limit_stats/detail/<string:detailKind>')
def apiLimitStatsDetailPath(detailKind: str):
    return _mono('api_limit_stats_detail_path')(detailKind)


@mainBp.route('/api/limit_stats/detail')
def apiLimitStatsDetail():
    return _mono('api_limit_stats_detail')()


# --- 3.3 ETF 表与序列（bias / mcd）---
@mainBp.route('/api/etf_bias_table')
def getEtfBiasTable():
    return _mono('get_etf_bias_table')()


@mainBp.route('/api/etf_ma_signal_table')
def getEtfMaSignalTable():
    return _mono('get_etf_ma_signal_table')()


@mainBp.route('/api/etf_bias_series')
def getEtfBiasSeries():
    return _mono('get_etf_bias_series')()


@mainBp.route('/api/etf_mcd_series')
def getEtfMcdSeries():
    return _mono('get_etf_mcd_series')()


# --- 3.4 其余首页 API ---
@mainBp.route('/api/idx_minute_diff')
def getIdxMinuteDiff():
    return _mono('get_idx_minute_diff')()


@mainBp.route('/api/idx_minute_diff_prev5')
def getIdxMinuteDiffPrev5():
    return _mono('get_idx_minute_diff_prev5')()


@mainBp.route('/api/top_moves')
def getTopMoves():
    return _mono('get_top_moves')()


@mainBp.route('/api/up_num_ma5_series')
def getUpNumMa5Series():
    return _mono('get_up_num_ma5_series')()


@mainBp.route('/api/amount_top5_pct_series')
def getAmountTop5PctSeries():
    return _mono('get_amount_top5_pct_series')()


@mainBp.route('/api/ma_sr_levels', methods=['GET', 'POST'])
def apiMaSrLevels():
    return _mono('api_ma_sr_levels')()


@mainBp.route('/api/etf_ma_signal_series')
def getEtfMaSignalSeries():
    return _mono('get_etf_ma_signal_series')()

# 代码更新时间：2026-06-17 15:27
