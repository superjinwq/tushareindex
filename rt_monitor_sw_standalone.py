#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
申万行业分析页 — 独立启动入口（团队参考）
========================================

在**不启动全量** `rt_monitor_app.py`（SocketIO、主监控页等）的前提下，仅暴露申万页所需路由：

  GET  /                          → 302 跳转 /rt-monitor/sw
  GET  /rt-monitor/sw             申万行业分析 HTML
  GET  /api/sw_industry_stats     行业聚合 JSON
  GET  /api/sw_index_amt_pctile_series  Amt% 弹窗序列 JSON
  GET  /health                    健康检查

业务 SQL / 缓存 / 财务聚合逻辑**复用**同目录 `rt_monitor_app.py`（import 时仍会加载该模块，
但不会执行其 `if __name__ == '__main__'` 启动流程）。

运行（仓库根目录）::

  python rt_monitor_sw_standalone.py

默认浏览器地址（端口默认 **5001**，避免与全量监控 5000 冲突）::

  http://127.0.0.1:5001/rt-monitor/sw

环境变量（与主监控一致，另增 SW 专用端口）::

  RT_MONITOR_SW_PORT      本脚本监听端口，默认 5001
  RT_MONITOR_SW_HOST      监听地址，默认 0.0.0.0
  RT_MONITOR_API_PREFIX   反代子路径前缀（无前导/尾随斜杠）
  DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME  MySQL（见 rt_monitor_app）

随仓库一并分发的文件（本单文件无法内嵌）::

  rt_monitor_app.py
  logger_config.py
  templates/rt_monitor_sw.html
  static/css/rt_monitor.css
  static/css/dashboard.css

排查：响应头 `X-RT-Monitor-Build` 应对齐 `rt_monitor_app.RT_MONITOR_STATIC_VER`；
若样式/接口像旧版，先确认无其它进程占用同一端口。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

# 供主程序将来识别「申万瘦启动」；当前 import rt_monitor_app 仍会加载全量模块定义
os.environ.setdefault("RT_MONITOR_SW_STANDALONE", "1")

from flask import Flask, jsonify, make_response, redirect, render_template, request

import rt_monitor.compat as mon
from logger_config import get_logger

logger = get_logger(__name__)

LISTEN_PORT = int(os.getenv("RT_MONITOR_SW_PORT", "5001"))
LISTEN_HOST = (os.getenv("RT_MONITOR_SW_HOST") or "0.0.0.0").strip() or "0.0.0.0"


def createSwStandaloneApp() -> Flask:
    """仅注册申万页与两个 API 的 Flask 应用。"""
    app = Flask(
        __name__,
        template_folder=str(_ROOT / "templates"),
        static_folder=str(_ROOT / "static"),
    )
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "rt-monitor-sw-standalone")
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    @app.context_processor
    def _injectRtMonitorTemplateGlobals():
        envp = (os.getenv("RT_MONITOR_API_PREFIX") or "").strip().rstrip("/")
        try:
            sr = (request.script_root or "").rstrip("/")
        except RuntimeError:
            sr = ""
        prefix = envp or sr
        return {
            "rt_monitor_static_ver": mon.RT_MONITOR_STATIC_VER,
            "rt_monitor_api_prefix": prefix,
        }

    @app.after_request
    def _noCacheSwJsonApi(resp):
        try:
            path = request.path or ""
        except RuntimeError:
            return resp
        if mon._request_path_matches_no_store_json_api(path):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
        return resp

    @app.route("/", strict_slashes=False)
    def rootRedirect():
        return redirect("/rt-monitor/sw", code=302)

    @app.route("/rt-monitor/sw", strict_slashes=False)
    def rtMonitorSwPage():
        resp = make_response(
            render_template(
                "rt_monitor_sw.html",
                sw_page_mode="sw",
                sw_api_path="/api/sw_industry_stats",
                sw_page_title="申万行业分析",
                sw_show_scheme_config=True,
                sw_index_options=[],
            )
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["X-RT-Monitor-Build"] = mon.RT_MONITOR_STATIC_VER
        resp.headers["X-RT-Monitor-App"] = "rt_monitor_sw_standalone"
        return resp

    app.add_url_rule(
        "/api/sw_industry_stats",
        endpoint="api_sw_industry_stats",
        view_func=mon.api_sw_industry_stats,
        methods=["GET"],
        strict_slashes=False,
    )
    app.add_url_rule(
        "/api/sw_index_amt_pctile_series",
        endpoint="api_sw_index_amt_pctile_series",
        view_func=mon.api_sw_index_amt_pctile_series,
        methods=["GET"],
        strict_slashes=False,
    )

    @app.route("/health", strict_slashes=False)
    def health():
        return jsonify(
            {
                "ok": True,
                "app": "rt_monitor_sw_standalone",
                "build": mon.RT_MONITOR_STATIC_VER,
                "routes": [
                    "/rt-monitor/sw",
                    "/api/sw_industry_stats",
                    "/api/sw_index_amt_pctile_series",
                ],
            }
        )

    return app


def _warmupSwIndustryCacheAsync(flaskApp: Flask) -> None:
    """后台预热默认 L1 视图，与 rt_monitor_app 启动预热口径一致。"""

    def _worker() -> None:
        try:
            time.sleep(1.0)
            with flaskApp.app_context():
                with flaskApp.test_client() as tc:
                    rv = tc.get("/api/sw_industry_stats?level=l1")
            if rv.status_code != 200:
                logger.warning(
                    "申万独立入口预热 HTTP status=%s（可忽略）",
                    getattr(rv, "status_code", "?"),
                )
                return
            data = rv.get_json(silent=True) or {}
            logger.info(
                "申万独立入口缓存已预热：L1 rows=%d",
                len(data.get("rows") or []),
            )
        except Exception as e:
            logger.warning("申万独立入口预热失败（可忽略）: %s", e)

    threading.Thread(
        target=_worker,
        daemon=True,
        name="warmup-sw-standalone",
    ).start()


def _ensureUtf8Stdout() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _ensureUtf8Stdout()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flaskApp = createSwStandaloneApp()
    _warmupSwIndustryCacheAsync(flaskApp)
    url = f"http://127.0.0.1:{LISTEN_PORT}/rt-monitor/sw"
    print("申万行业分析 — 独立入口（团队参考）")
    print(f"  页面: {url}")
    print(f"  构建: {mon.RT_MONITOR_STATIC_VER}")
    print(f"  监听: {LISTEN_HOST}:{LISTEN_PORT}")
    print("  说明: 业务逻辑见 rt_monitor.monolith；全量监控请运行 run_rt_monitor.bat")
    flaskApp.run(
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# 代码更新时间：2026-06-17 19:56
