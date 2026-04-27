#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时数据展示Web服务
功能：提供融资融券行业分布数据的实时网页展示
作者：AI Assistant
版本：v1.0
日期：2025-01-27
"""

import os
import sys
import json
import pandas as pd
import pymysql
from datetime import datetime
from flask import Flask, render_template, jsonify, make_response, request
from flask_socketio import SocketIO, emit
import threading
import time
import warnings

# 导入现有模块
from database_pool import get_db_connection, retry_with_backoff
from logger_config import get_logger

warnings.filterwarnings('ignore')

# 初始化日志
logger = get_logger(__name__)

# 数据库配置
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', '3306')),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', '123456'),
    'database': os.getenv('DB_NAME', 'stock_wind'),
    'charset': 'utf8mb4',
    'autocommit': False,
    'connect_timeout': 10,
    'read_timeout': 60,
    'write_timeout': 60
}
# margin_daily 在 stock_tushare，与 rt_monitor_app._query_margin_daily_recent 口径一致
DB_CONFIG_TUSHARE = {
    **DB_CONFIG,
    'database': os.getenv('DB_NAME_TUSHARE', 'stock_tushare'),
    'read_timeout': int(os.getenv('DB_READ_TIMEOUT_TUSHARE', '180')),
    'write_timeout': int(os.getenv('DB_WRITE_TIMEOUT_TUSHARE', '180')),
}

# 数据刷新配置（单位：秒）
UPDATE_INTERVAL = int(os.getenv('UPDATE_INTERVAL', '30'))  # 默认30秒更新一次

# 与 rt_monitor_app 共用 templates/static 绝对路径，避免工作目录不对时读到旧模板或空白变量
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
RT_MONITOR_STATIC_VER = '20260427-22'  # 与 rt_monitor_app.RT_MONITOR_STATIC_VER 保持同步（静态资源 ?v=）

app = Flask(
    __name__,
    template_folder=os.path.join(_WEB_DIR, 'templates'),
    static_folder=os.path.join(_WEB_DIR, 'static'),
)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
socketio = SocketIO(app, cors_allowed_origins="*")


@app.after_request
def _no_cache_margin_daily_json(resp):
    """与 rt_monitor_app 一致：禁止缓存融资融券序列接口，避免路径级缓存拿到旧响应。"""
    try:
        path = request.path or ''
    except RuntimeError:
        return resp
    if path in (
        '/api/margin_daily_recent',
        '/api/margin_rzmre_idx_series',
        '/api/etf_bias_series',
        '/api/etf_mcd_series',
    ):
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
    return resp


@app.context_processor
def _inject_rt_monitor_template_globals():
    """与 rt_monitor_app 一致：静态版本号 + 行情页 API 路径前缀（反代子路径）。"""
    envp = (os.getenv('RT_MONITOR_API_PREFIX') or '').strip().rstrip('/')
    try:
        sr = (request.script_root or '').rstrip('/')
    except RuntimeError:
        sr = ''
    prefix = envp or sr
    return {
        'rt_monitor_static_ver': RT_MONITOR_STATIC_VER,
        'rt_monitor_api_prefix': prefix,
    }

# 全局数据缓存
data_cache = {
    'latest_date': None,
    'industry_distribution': None,
    'statistics': None,
    'unclassified_codes': None,
    'last_update': None
}

# 行情涨跌中位数缓存（接口独立，按需刷新）
rt_median_cache = {}
rt_median_cache_ts = 0
RT_MEDIAN_CACHE_TTL = 10  # 秒

# 数据更新锁
update_lock = threading.Lock()


class DataFetcher:
    """数据获取类"""

    def __init__(self):
        self.logger = logger

    def get_latest_date(self):
        """获取最新日期"""
        def _get_latest_date():
            with get_db_connection() as conn:
                query = "SELECT MAX(trade_date) as latest_date FROM margin_detail"
                df = pd.read_sql(query, conn)
                if not df.empty and df.iloc[0]['latest_date'] is not None:
                    return str(df.iloc[0]['latest_date'])
                return None

        try:
            latest_date = retry_with_backoff(_get_latest_date)
            return latest_date
        except Exception as e:
            self.logger.error(f"获取最新日期失败: {e}")
            return None

    def get_industry_distribution(self, trade_date):
        """获取行业分布数据"""
        def _get_distribution():
            with get_db_connection() as conn:
                query = """
                SELECT 
                    CASE 
                        WHEN s.industry_sw_2021_2 IS NOT NULL THEN s.industry_sw_2021_2
                        WHEN fb.fund_name IS NOT NULL AND UPPER(fb.fund_name) LIKE '%%ETF%%' THEN 'ETF'
                        ELSE '未分类'
                    END as industry,
                    COUNT(md.ts_code) as record_count,
                    SUM(md.rzye) as total_rzye,
                    AVG(md.rzye) as avg_rzye,
                    MAX(md.rzye) as max_rzye,
                    MIN(md.rzye) as min_rzye,
                    SUM(md.rzjmre) as total_rzjmre,
                    SUM(md.rzjmre_t3) as total_rzjmre_t3,
                    SUM(md.rzjmre_t5) as total_rzjmre_t5,
                    SUM(md.rzjmre_t10) as total_rzjmre_t10,
                    SUM(md.rzjmre_t20) as total_rzjmre_t20,
                    SUM(md.rzjmre_t60) as total_rzjmre_t60
                FROM margin_detail md
                LEFT JOIN stockcodes s ON md.ts_code = s.windcodes
                LEFT JOIN fund_basic fb ON md.ts_code = fb.ts_code
                WHERE md.trade_date = %s
                  AND md.rzye IS NOT NULL
                GROUP BY 
                    CASE 
                        WHEN s.industry_sw_2021_2 IS NOT NULL THEN s.industry_sw_2021_2
                        WHEN fb.fund_name IS NOT NULL AND UPPER(fb.fund_name) LIKE '%%ETF%%' THEN 'ETF'
                        ELSE '未分类'
                    END
                ORDER BY SUM(md.rzye) DESC
                """
                df = pd.read_sql(query, conn, params=[trade_date])
                return df

        try:
            df = retry_with_backoff(_get_distribution)
            if df.empty:
                return None

            numeric_cols = ['total_rzye', 'avg_rzye', 'max_rzye', 'min_rzye',
                          'total_rzjmre', 'total_rzjmre_t3', 'total_rzjmre_t5',
                          'total_rzjmre_t10', 'total_rzjmre_t20', 'total_rzjmre_t60']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

            return df.to_dict('records')
        except Exception as e:
            self.logger.error(f"获取行业分布失败: {e}")
            return None

    def get_statistics(self, trade_date):
        """获取统计信息"""
        def _get_stats():
            with get_db_connection() as conn:
                query_total = "SELECT COUNT(*) as total_count FROM margin_detail WHERE trade_date = %s"
                df_total = pd.read_sql(query_total, conn, params=[trade_date])
                total_records = int(df_total.iloc[0]['total_count']) if not df_total.empty else 0

                query_matched = """
                SELECT COUNT(*) as matched_count
                FROM margin_detail md
                INNER JOIN stockcodes s ON md.ts_code = s.windcodes
                WHERE md.trade_date = %s
                """
                df_matched = pd.read_sql(query_matched, conn, params=[trade_date])
                matched_records = int(df_matched.iloc[0]['matched_count']) if not df_matched.empty else 0

                query_unmatched = """
                SELECT COUNT(*) as unmatched_count
                FROM margin_detail md
                LEFT JOIN stockcodes s ON md.ts_code = s.windcodes
                WHERE md.trade_date = %s AND s.windcodes IS NULL
                """
                df_unmatched = pd.read_sql(query_unmatched, conn, params=[trade_date])
                unmatched_records = int(df_unmatched.iloc[0]['unmatched_count']) if not df_unmatched.empty else 0

                query_etf = """
                SELECT COUNT(*) as etf_count
                FROM margin_detail md
                LEFT JOIN stockcodes s ON md.ts_code = s.windcodes
                LEFT JOIN fund_basic fb ON md.ts_code = fb.ts_code
                WHERE md.trade_date = %s
                  AND md.rzye IS NOT NULL
                  AND (s.windcodes IS NULL OR s.industry_sw_2021_2 IS NULL)
                  AND fb.ts_code IS NOT NULL
                  AND UPPER(fb.fund_name) LIKE '%%ETF%%'
                """
                df_etf = pd.read_sql(query_etf, conn, params=[trade_date])
                etf_records = int(df_etf.iloc[0]['etf_count']) if not df_etf.empty else 0

                return {
                    'total_records': total_records,
                    'matched_records': matched_records,
                    'unmatched_records': unmatched_records,
                    'etf_records': etf_records
                }

        try:
            stats = retry_with_backoff(_get_stats)
            return stats
        except Exception as e:
            self.logger.error(f"获取统计信息失败: {e}")
            return {}

    def get_unclassified_codes(self, trade_date, limit=10):
        """获取未分类股票代码"""
        def _get_unclassified():
            with get_db_connection() as conn:
                query = """
                SELECT 
                    md.ts_code,
                    COALESCE(s.sec_name, fb.fund_name, 'N/A') as sec_name,
                    md.rzye,
                    CASE 
                        WHEN s.windcodes IS NULL AND fb.ts_code IS NULL THEN '未匹配到stockcodes和fund_basic表'
                        WHEN s.windcodes IS NULL AND fb.ts_code IS NOT NULL AND UPPER(fb.fund_name) NOT LIKE '%%ETF%%' THEN '匹配到fund_basic但fund_name不包含ETF'
                        WHEN s.windcodes IS NOT NULL AND s.industry_sw_2021_2 IS NULL AND (fb.ts_code IS NULL OR UPPER(fb.fund_name) NOT LIKE '%%ETF%%') THEN '行业字段为空且非ETF'
                        ELSE '其他'
                    END as unclassified_reason
                FROM margin_detail md
                LEFT JOIN stockcodes s ON md.ts_code = s.windcodes
                LEFT JOIN fund_basic fb ON md.ts_code = fb.ts_code
                WHERE md.trade_date = %s
                  AND md.rzye IS NOT NULL
                  AND (
                      (s.windcodes IS NULL OR s.industry_sw_2021_2 IS NULL)
                      AND (fb.ts_code IS NULL OR UPPER(fb.fund_name) NOT LIKE '%%ETF%%')
                  )
                ORDER BY md.rzye DESC
                LIMIT %s
                """
                df = pd.read_sql(query, conn, params=[trade_date, limit])
                return df.to_dict('records')

        try:
            return retry_with_backoff(_get_unclassified)
        except Exception as e:
            self.logger.error(f"获取未分类股票代码失败: {e}")
            return []

    def get_rt_median(self):
        """从 v_rt_k_with_category 查询 A股/ETF 涨跌幅中位数"""
        def _query_and_compute():
            with get_db_connection() as conn:
                query = """
                SELECT major_type, pct_change
                FROM v_rt_k_with_category
                WHERE major_type IN ('A股', 'ETF') AND pct_change IS NOT NULL
                """
                df = pd.read_sql(query, conn)
            if df.empty:
                return {'error': '视图无有效数据，请确认 rt_k 与 rt_k_code_category 已就绪'}
            stock_df = df[df['major_type'] == 'A股']['pct_change']
            etf_df = df[df['major_type'] == 'ETF']['pct_change']
            stock_median = float(stock_df.median()) if len(stock_df) else None
            etf_median = float(etf_df.median()) if len(etf_df) else None
            return {
                'stock_median_pct': round(stock_median, 2) if stock_median is not None else None,
                'etf_median_pct': round(etf_median, 2) if etf_median is not None else None,
                'stock_count': int(len(stock_df)),
                'etf_count': int(len(etf_df)),
                'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

        try:
            return retry_with_backoff(_query_and_compute)
        except Exception as e:
            self.logger.exception("获取行情涨跌中位数失败")
            err_msg = str(e)
            if 'v_rt_k_with_category' in err_msg or 'View' in err_msg or 'exist' in err_msg.lower():
                return {'error': '视图未就绪，请先运行 rt_k_代码分类.py'}
            return {'error': f'查询失败: {err_msg}'}


# 初始化数据获取器
data_fetcher = DataFetcher()


def update_data():
    """更新融资融券数据缓存"""
    global data_cache

    with update_lock:
        try:
            logger.info("开始更新数据...")

            latest_date = data_fetcher.get_latest_date()
            if not latest_date:
                logger.warning("未找到最新日期")
                return

            if data_cache['latest_date'] == latest_date and data_cache['industry_distribution'] is not None:
                logger.info("数据未变化，跳过更新")
                return

            industry_dist = data_fetcher.get_industry_distribution(latest_date)
            stats = data_fetcher.get_statistics(latest_date)
            unclassified = data_fetcher.get_unclassified_codes(latest_date, limit=10)

            data_cache['latest_date'] = latest_date
            data_cache['industry_distribution'] = industry_dist
            data_cache['statistics'] = stats
            data_cache['unclassified_codes'] = unclassified
            data_cache['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            logger.info(f"数据更新完成: {latest_date}")

            socketio.emit('data_update', {
                'latest_date': latest_date,
                'last_update': data_cache['last_update']
            })

        except Exception as e:
            logger.error(f"更新数据失败: {e}")


def ensure_rt_k_classification():
    """启动时确保 rt_k 分类表与视图存在（供行情监控页使用）"""
    try:
        from rt_k_代码分类 import run_classification
        run_classification(silent=True)
        logger.info("rt_k 分类已就绪（rt_k_code_category / v_rt_k_with_category）")
    except Exception as e:
        logger.warning("rt_k 分类未执行: %s", e)


def background_update_thread():
    """后台数据更新线程"""
    from database_pool import pool_manager
    pool_manager._default_db_config = DB_CONFIG
    pool_manager._default_pool_size = 5
    pool_manager._default_max_overflow = 10
    pool_manager._default_pool_timeout = 10
    pool_manager._default_pool_recycle = 1800

    pool = pool_manager.get_pool('default')
    logger.info(f"连接池已初始化: {pool.get_stats()}")

    ensure_rt_k_classification()

    update_data()

    while True:
        time.sleep(UPDATE_INTERVAL)
        update_data()


def _to_float_or_none(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _query_margin_daily_recent(limit: int = 300):
    """只读 margin_daily：rzye_sum、netbuy 库内单位为元，接口字段 *_yi 为亿元（与 rt_monitor_app 同源 SQL）。"""
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（margin_daily）: %s", e)
        return {'rows': [], 'error': '数据库连接失败'}
    try:
        lim = int(limit)
        lim = max(1, min(lim, 1300))
    except (TypeError, ValueError):
        lim = 300
    sql = (
        "SELECT trade_date, "
        "ROUND(rzye_sum / 100000000, 2) AS rzye_sum_yi, "
        "ROUND(netbuy / 100000000, 2) AS netbuy_yi, "
        "ROUND(netbuy_pct, 2) AS netbuy_pct "
        "FROM margin_daily ORDER BY trade_date DESC LIMIT %s"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (lim,))
            raw = cur.fetchall()
        rows = []
        for r in raw or []:
            td = r[0]
            rzye_yi = _to_float_or_none(r[1])
            nb_yi = _to_float_or_none(r[2])
            nb_pct = _to_float_or_none(r[3])
            td_str = td.isoformat() if hasattr(td, 'isoformat') else str(td)
            rows.append({
                'trade_date': td_str,
                'rzye_sum_yi': rzye_yi,
                'netbuy_yi': nb_yi,
                'netbuy_pct': nb_pct,
            })
        return {
            'rows': rows,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        logger.exception("查询 margin_daily 失败: %s", e)
        return {'rows': [], 'error': str(e)}
    finally:
        conn.close()


def _query_margin_rzmre_idx_series(limit: int = 300):
    """margin_daily.rzmre_pct 与 index_daily(399317.SZ).close 按 trade_date 对齐（与 rt_monitor_app 同源 SQL）。"""
    idx_ts = '399317.SZ'
    try:
        conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    except Exception as e:
        logger.exception("连接 stock_tushare 失败（margin_rzmre_idx_series）: %s", e)
        return {'rows': [], 'error': '数据库连接失败'}
    try:
        lim = int(limit)
        lim = max(1, min(lim, 1300))
    except (TypeError, ValueError):
        lim = 300
    sql = (
        "SELECT m.trade_date, m.rzmre_pct, m.rzmre_pct_per, i.close AS idx_close "
        "FROM ("
        "  SELECT trade_date, rzmre_pct, rzmre_pct_per FROM margin_daily "
        "  ORDER BY trade_date DESC LIMIT %s"
        ") m "
        "INNER JOIN index_daily i ON i.ts_code = %s AND i.trade_date = m.trade_date "
        "ORDER BY m.trade_date ASC"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (lim, idx_ts))
            raw = cur.fetchall()
        rows = []
        for r in raw or []:
            td = r[0]
            pct = _to_float_or_none(r[1])
            per = _to_float_or_none(r[2])
            cl = _to_float_or_none(r[3])
            td_str = td.isoformat() if hasattr(td, 'isoformat') else str(td)
            rows.append({
                'trade_date': td_str,
                'rzmre_pct': pct,
                'rzmre_pct_per': per,
                'idx_close': cl,
            })
        return {
            'rows': rows,
            'ts_code': idx_ts,
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        logger.exception("查询 margin_rzmre_idx_series 失败: %s", e)
        return {'rows': [], 'error': str(e)}
    finally:
        conn.close()


# Flask路由
@app.route('/')
def index():
    """主页 - 融资融券行业分布"""
    return render_template('dashboard.html')


@app.route('/rt-monitor')
def rt_monitor():
    """行情监控页（与 rt_monitor_app 共用模板；已含 /api/margin_daily_recent 等，完整 API 仍以 rt_monitor_app 为准）"""
    resp = make_response(render_template('rt_monitor.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'web_dashboard'
    return resp


@app.route('/api/data')
def get_data():
    """获取融资融券数据API"""
    return jsonify(data_cache)


@app.route('/api/refresh')
def refresh_data():
    """手动刷新融资融券数据"""
    update_data()
    return jsonify({'status': 'success', 'message': '数据已刷新'})


@app.route('/api/rt_median')
def get_rt_median():
    """行情涨跌中位数 API"""
    global rt_median_cache, rt_median_cache_ts
    now = time.time()
    if rt_median_cache and (now - rt_median_cache_ts) < RT_MEDIAN_CACHE_TTL and 'error' not in rt_median_cache:
        return jsonify(rt_median_cache)
    result = data_fetcher.get_rt_median()
    rt_median_cache = result
    rt_median_cache_ts = now
    return jsonify(result)


@app.route('/api/margin_daily_recent', methods=['GET'], strict_slashes=False)
def get_margin_daily_recent():
    """融资融券按日汇总（与 rt_monitor_app 同路径）；limit 默认 300、上限 1300。"""
    try:
        try:
            limit = int(request.args.get('limit', default='300'))
        except (TypeError, ValueError):
            limit = 300
        payload = _query_margin_daily_recent(limit=limit)
        status = 200 if not payload.get('error') else 500
        return jsonify(payload), status
    except Exception as e:
        logger.exception("margin_daily_recent 接口异常: %s", e)
        return jsonify({'rows': [], 'error': str(e)}), 500


@app.route('/api/margin_rzmre_idx_series', methods=['GET'], strict_slashes=False)
def get_margin_rzmre_idx_series():
    """融资买入额占比 + 国证A指收盘（与 rt_monitor_app 同路径）。"""
    try:
        try:
            limit = int(request.args.get('limit', default='300'))
        except (TypeError, ValueError):
            limit = 300
        payload = _query_margin_rzmre_idx_series(limit=limit)
        status = 200 if not payload.get('error') else 500
        return jsonify(payload), status
    except Exception as e:
        logger.exception("margin_rzmre_idx_series 接口异常: %s", e)
        return jsonify({'rows': [], 'error': str(e)}), 500


# WebSocket事件
@socketio.on('connect')
def handle_connect():
    logger.info('客户端已连接')
    emit('connected', {'message': '连接成功'})


@socketio.on('request_data')
def handle_request_data():
    emit('data_response', data_cache)


# 启动后台更新线程
update_thread = threading.Thread(target=background_update_thread, daemon=True)
update_thread.start()


if __name__ == '__main__':
    logger.info("Web服务启动（web_dashboard.py：融资融券 + /rt-monitor + margin 系列接口）")
    logger.info("融资融券监控: http://localhost:5000")
    logger.info("行情监控: http://localhost:5000/rt-monitor")
    logger.info(
        "rt_monitor 模板: %s  构建: %s",
        os.path.join(_WEB_DIR, 'templates', 'rt_monitor.html'),
        RT_MONITOR_STATIC_VER,
    )
    try:
        _md_ok = any(str(r.rule) == '/api/margin_daily_recent' for r in app.url_map.iter_rules())
        _mrz_ok = any(str(r.rule) == '/api/margin_rzmre_idx_series' for r in app.url_map.iter_rules())
        logger.info(
            "融资融券 /api/margin_daily_recent: %s；/api/margin_rzmre_idx_series: %s  构建: %s",
            _md_ok,
            _mrz_ok,
            RT_MONITOR_STATIC_VER,
        )
    except Exception:
        pass
    logger.info("若需完整行情监控 API（全部路由），请运行: python rt_monitor_app.py（勿与 web_dashboard 同端口并发）")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)

# 代码更新时间：2026-04-13 17:48 同步 RT_MONITOR_STATIC_VER=20260413-21（轮询稳定性修复版本）
# 代码更新时间：2026-04-14 08:22 同步 RT_MONITOR_STATIC_VER=20260414-16（应收账款表 CSS）
# 代码更新时间：2026-04-14 08:26 同步 RT_MONITOR_STATIC_VER=20260414-17（Top30 三表）
# 代码更新时间：2026-04-14 08:32 同步 RT_MONITOR_STATIC_VER=20260414-18（Top30 列缝）
# 代码更新时间：2026-04-14 08:36 同步 RT_MONITOR_STATIC_VER=20260414-19（Top30 代码名称间距）
# 代码更新时间：2026-04-14 08:40 同步 RT_MONITOR_STATIC_VER=20260414-20（Top30 幅度与申万间距）
# 代码更新时间：2026-04-14 08:59 同步 RT_MONITOR_STATIC_VER=20260414-21（涨跌家数表紧凑）
# 代码更新时间：2026-04-14 09:01 同步 RT_MONITOR_STATIC_VER=20260414-22（涨跌家数与成交额表行高）
# 代码更新时间：2026-04-14 同步 RT_MONITOR_STATIC_VER=20260414-25（应收账款双行表头 + CSS）
# 代码更新时间：2026-04-14 同步 RT_MONITOR_STATIC_VER=20260414-26（应收账款毛利率回退 + 顶栏栅格）
# 代码更新时间：2026-04-14 同步 RT_MONITOR_STATIC_VER=20260414-27（应收账款 thead 分组对齐）
# 代码更新时间：2026-04-14 同步 RT_MONITOR_STATIC_VER=20260414-28（fi_gpm+竖线）
# 代码更新时间：2026-04-14 同步 RT_MONITOR_STATIC_VER=20260414-29（应收账款 thead 固定版式）
# 代码更新时间：2026-04-14 同步 RT_MONITOR_STATIC_VER=20260414-30（应收账款 ROE 四列）
# 代码更新时间：2026-04-14 19:38 同步 RT_MONITOR_STATIC_VER=20260414-33（应收账款两组经营现金流指标）
# 代码更新时间：2026-04-14 21:35 同步 RT_MONITOR_STATIC_VER=20260414-35（应收账款公告日期四列）
# 代码更新时间：2026-04-14 22:29 同步 RT_MONITOR_STATIC_VER=20260414-40（合同负债新增 Chg% 列）
# 代码更新时间：2026-04-14 22:32 同步 RT_MONITOR_STATIC_VER=20260414-41（Chg% 负值绿色）
# 代码更新时间：2026-04-15 09:09 同步 RT_MONITOR_STATIC_VER=20260414-42（应收账款筛选方案弹窗「重置筛选」）
# 代码更新时间：2026-04-15 09:22 同步 RT_MONITOR_STATIC_VER=20260414-43（应收账款表左四列冻结）
# 代码更新时间：2026-04-15 11:50 同步 RT_MONITOR_STATIC_VER=20260414-44（应收账款表加载失败仍填充报告期下拉）
# 代码更新时间：2026-04-15 12:21 同步 RT_MONITOR_STATIC_VER=20260415-45（应收账款表应收占比 TTM 列）
# 代码更新时间：2026-04-15 12:26 同步 RT_MONITOR_STATIC_VER=20260415-46（应收账款表头 TTM/应收% 互换）
# 代码更新时间：2026-04-15 12:35 同步 RT_MONITOR_STATIC_VER=20260415-47（应收账款表体分组竖线）
# 代码更新时间：2026-04-15 12:49 同步 RT_MONITOR_STATIC_VER=20260415-48（经营现金流/营业收入×100 与表头%）
# 代码更新时间：2026-04-15 12:55 同步 RT_MONITOR_STATIC_VER=20260415-49（ocf_to_or 精度与四位显示）
# 代码更新时间：2026-04-15 13:05 同步 RT_MONITOR_STATIC_VER=20260415-50（应收表竖线+OCF 显示）
# 代码更新时间：2026-04-15 13:02 同步 RT_MONITOR_STATIC_VER=20260415-51（ocf_to_or×100 两位小数）
# 代码更新时间：2026-04-15 13:09 同步 RT_MONITOR_STATIC_VER=20260415-52（ocf_to_or Decimal 两位）
# 代码更新时间：2026-04-15 13:27 同步 RT_MONITOR_STATIC_VER=20260415-53（应收表代码名称居中）
# 代码更新时间：2026-04-15 13:46 同步 RT_MONITOR_STATIC_VER=20260415-54（rt_median 稳定列合并）
# 代码更新时间：2026-04-15 13:53 同步 RT_MONITOR_STATIC_VER=20260415-55（应收表代码名称列宽）
# 代码更新时间：2026-04-15 17:23 同步 RT_MONITOR_STATIC_VER=20260415-56（应收表新增两组九季度同比）
# 代码更新时间：2026-04-15 20:26 同步 RT_MONITOR_STATIC_VER=20260415-57（应收表超时修复）
# 代码更新时间：2026-04-15 20:39 同步 RT_MONITOR_STATIC_VER=20260415-58（应收表分组顺序调整）
# 代码更新时间：2026-04-15 21:09 同步 RT_MONITOR_STATIC_VER=20260415-59（应收表分组对调）
# 代码更新时间：2026-04-15 21:25 同步 RT_MONITOR_STATIC_VER=20260415-60（应收表顶部同步滚动条）
# 代码更新时间：2026-04-15 21:46 同步 RT_MONITOR_STATIC_VER=20260415-61（应收表容器宽度修复）
# 代码更新时间：2026-04-15 22:09 同步 RT_MONITOR_STATIC_VER=20260415-62（应收表首行高度压缩）
# 代码更新时间：2026-04-15 22:45 同步 RT_MONITOR_STATIC_VER=20260415-63（Chart.js 缺失时隐藏走势图区块）
# 代码更新时间：2026-04-15 22:53 同步 RT_MONITOR_STATIC_VER=20260415-64（走势图区块默认隐藏，Chart 可用再显示）
# 代码更新时间：2026-04-15 22:59 同步 RT_MONITOR_STATIC_VER=20260415-65（图表库本地优先+双 CDN 回退）
# 代码更新时间：2026-04-15 23:42 同步 RT_MONITOR_STATIC_VER=20260415-66（隐藏走势图行使用 !important 覆盖布局样式）
# 代码更新时间：2026-04-16 06:40 同步 RT_MONITOR_STATIC_VER=20260416-1（修复六张走势图被误隐藏）
# 代码更新时间：2026-04-17 09:09 同步 RT_MONITOR_STATIC_VER=20260417-4（static/vendor 图表库落地，与主监控一致）
# 代码更新时间：2026-04-17 09:25 同步 RT_MONITOR_STATIC_VER=20260417-5（chart-stack 单文件 + defer）
# 代码更新时间：2026-04-17 09:40 同步 RT_MONITOR_STATIC_VER=20260417-6（财报页 defer + 跳过 median-quad）
# 代码更新时间：2026-04-17 09:29 同步 RT_MONITOR_STATIC_VER=20260417-7（财报瘦包 rt_monitor_fina.js）
# 代码更新时间：2026-04-17 11:33 同步 RT_MONITOR_STATIC_VER=20260417-8（财报固定资产-单季营收弹窗）
# 代码更新时间：2026-04-17 11:41 同步 RT_MONITOR_STATIC_VER=20260417-9（固定资产弹窗 msg 定位修复）
# 代码更新时间：2026-04-17 11:47 同步 RT_MONITOR_STATIC_VER=20260417-10（initFinaMetricPanel tbody 修复）
# 代码更新时间：2026-04-17 12:02 同步 RT_MONITOR_STATIC_VER=20260417-11（固定资产弹窗单季营收+柱状图）
# 代码更新时间：2026-04-17 12:15 同步 RT_MONITOR_STATIC_VER=20260417-12（弹窗去 caption、单坐标轴）
# 代码更新时间：2026-04-17 12:28 同步 RT_MONITOR_STATIC_VER=20260417-13（固定资产弹窗展示代码名称与手动输入）
# 代码更新时间：2026-04-17 12:20 同步 RT_MONITOR_STATIC_VER=20260417-14（全站可 resize 弹窗 localStorage 几何持久化）
# 代码更新时间：2026-04-17 12:29 同步 RT_MONITOR_STATIC_VER=20260417-15（固定资产弹窗查询区与标题行版式）
# 代码更新时间：2026-04-17 12:35 同步 RT_MONITOR_STATIC_VER=20260417-16（固定资产弹窗表头合并与紧凑查询行）
# 代码更新时间：2026-04-17 12:42 同步 RT_MONITOR_STATIC_VER=20260417-17（固定资产表头与输入同行、去加载按钮）
# 代码更新时间：2026-04-26 17:30 同步 RT_MONITOR_STATIC_VER=20260426-11（与 rt_monitor_app 静态资源版本一致）
# 代码更新时间：2026-04-26 17:45 同步 RT_MONITOR_STATIC_VER=20260426-12
# 代码更新时间：2026-04-26 18:05 同步 RT_MONITOR_STATIC_VER=20260426-13
# 代码更新时间：2026-04-26 20:31 同步 RT_MONITOR_STATIC_VER=20260426-14（财报方案摘要 CSS/JS）
# 代码更新时间：2026-04-26 21:05 同步 RT_MONITOR_STATIC_VER=20260426-15
# 代码更新时间：2026-04-26 21:27 同步 RT_MONITOR_STATIC_VER=20260426-16
# 代码更新时间：2026-04-26 21:41 同步 RT_MONITOR_STATIC_VER=20260426-17
# 代码更新时间：2026-04-26 22:36 同步 RT_MONITOR_STATIC_VER=20260426-18（财报表 v0..v8 列）
# 代码更新时间：2026-04-26 23:05 同步 RT_MONITOR_STATIC_VER=20260426-19（v 列口径修正）
# 代码更新时间：2026-04-27 08:24 同步 RT_MONITOR_STATIC_VER=20260426-20（冻结列股票名称打开固定资产速览）
# 代码更新时间：2026-04-27 14:24 同步 RT_MONITOR_STATIC_VER=20260427-21（单季度毛利率新增新高/环比）
# 代码更新时间：2026-04-27 14:35 同步 RT_MONITOR_STATIC_VER=20260427-22（修复毛利率新高/环比显示与表头错位）
