#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行情监控 Web 服务入口（薄层）。

- 路由与业务逻辑：rt_monitor.monolith（P1 起按域迁入 rt_monitor.blueprints / services）
- 启动：run_rt_monitor.bat → 本文件 → rt_monitor.app_factory.run_server

运行: python rt_monitor_app.py
访问: http://localhost:5000（RT_MONITOR_PORT）
"""
from __future__ import annotations

from rt_monitor.app_factory import create_app, run_server
from rt_monitor.core.config import (
    DB_CONFIG,
    DB_CONFIG_TUSHARE,
    RT_MONITOR_LISTEN_PORT,
    RT_MONITOR_STATIC_VER,
)

app, socketio = create_app()

# 外部脚本兼容（roic_*、export_*）；优先从 fina 查询模块导入
from rt_monitor.services.fina_arturn_queries import (
    _fina_end_date_str_from_yq,
    _fina_prev_yq,
    _fina_quarter_single_revenue,
)
from rt_monitor.monolith import _normalize_end_date_yyyymmdd

if __name__ == '__main__':
    run_server()

# 代码更新时间：2026-06-17 17:35
