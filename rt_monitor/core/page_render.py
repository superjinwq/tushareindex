"""行情监控子页 HTML 渲染（禁止缓存 + 构建头）。"""
from __future__ import annotations

from flask import make_response, render_template

from rt_monitor.core.config import RT_MONITOR_STATIC_VER


def renderRtMonitorPageNoCache(templateName: str):
    resp = make_response(render_template(templateName))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['X-RT-Monitor-Build'] = RT_MONITOR_STATIC_VER
    resp.headers['X-RT-Monitor-App'] = 'rt_monitor_app'
    return resp

# 代码更新时间：2026-06-17 22:58
