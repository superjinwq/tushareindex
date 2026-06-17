"""rt_monitor 环境与路径配置（单点引用，避免 monolith 与 factory 重复定义）。"""
from __future__ import annotations

import os

# 仓库根目录（rt_monitor 包的上一级，即含 templates/ static/ 的目录）
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(_PKG_DIR)

RT_MONITOR_LISTEN_PORT = int(os.getenv('RT_MONITOR_PORT', '5000'))

# 静态资源版本号：更新 css/js 后递增
RT_MONITOR_STATIC_VER = '20260617-1'

DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', '3306')),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', '123456'),
    'database': os.getenv('DB_NAME', 'stock_wind'),
    'charset': 'utf8mb4',
    'autocommit': False,
    'connect_timeout': 10,
    'read_timeout': 8,
    'write_timeout': 10,
}

DB_CONFIG_TUSHARE = {
    **DB_CONFIG,
    'database': os.getenv('DB_NAME_TUSHARE', 'stock_tushare'),
    'read_timeout': int(os.getenv('DB_READ_TIMEOUT_TUSHARE', '180')),
    'write_timeout': int(os.getenv('DB_WRITE_TIMEOUT_TUSHARE', '180')),
}


def appPath(*parts: str) -> str:
    return os.path.join(REPO_ROOT, *parts)


MA_SR_LEVELS_PATH = appPath('ma_sr_levels.json')
APP_LOCK_FILE = appPath('.rt_monitor_app.lock')
NIUSAN_HOLDER_CONFIG_PATH = appPath('niusan_holder_config.json')

# 代码更新时间：2026-06-17 15:48
