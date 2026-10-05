"""rt_monitor 环境与路径配置（单点引用，避免 monolith 与 factory 重复定义）。"""
from __future__ import annotations

import hashlib
import os

# 仓库根目录（rt_monitor 包的上一级，即含 templates/ static/ 的目录）
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(_PKG_DIR)

RT_MONITOR_LISTEN_PORT = int(os.getenv('RT_MONITOR_PORT', '5000'))

# 静态资源版本号：static/ + templates/（含模板内联 JS/CSS）内容哈希，改任一资源即自动变化，
# 用于 ?v= 缓存失效与响应头 X-RT-Monitor-Build。环境变量 RT_MONITOR_STATIC_VER 非空时手动覆盖。
# 手工递增时代的历史见 rt_monitor/CHANGELOG-static-ver.md。
STATIC_VER_SOURCE_DIRS = ('static', 'templates')


def computeRtMonitorStaticVer(repoRoot: str = REPO_ROOT) -> str:
    """按相对路径排序，对「相对路径 + 文件字节」做 sha1，返回 'c' + 前 10 位。"""
    h = hashlib.sha1()
    for sub in STATIC_VER_SOURCE_DIRS:
        base = os.path.join(repoRoot, sub)
        for root, dirs, files in os.walk(base):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, repoRoot).replace(os.sep, '/')
                h.update(rel.encode('utf-8') + b'\0')
                with open(full, 'rb') as fh:
                    h.update(fh.read())
                h.update(b'\0')
    return 'c' + h.hexdigest()[:10]


RT_MONITOR_STATIC_VER = os.getenv('RT_MONITOR_STATIC_VER', '').strip() or computeRtMonitorStaticVer()

_dbPassword = os.getenv('DB_PASSWORD', '').strip()
if not _dbPassword:
    raise SystemExit('缺少环境变量 DB_PASSWORD')

DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', '3306')),
    'user': os.getenv('DB_USER', 'root'),
    'password': _dbPassword,
    'database': os.getenv('DB_NAME', 'stock_wind'),
    'charset': 'utf8mb4',
    'autocommit': False,
    'connect_timeout': 10,
    'read_timeout': 8,
    'write_timeout': 10,
}

# 申万页 stockstat / HL_C 等批量 JOIN（成分股 5000+）需更长读超时；默认 120s，可用环境变量覆盖
DB_CONFIG_WIND_BULK = {
    **DB_CONFIG,
    'read_timeout': int(os.getenv('DB_READ_TIMEOUT_WIND', '120')),
    'write_timeout': int(os.getenv('DB_WRITE_TIMEOUT_WIND', '120')),
}

DB_CONFIG_TUSHARE = {
    **DB_CONFIG,
    'database': os.getenv('DB_NAME_TUSHARE', 'stock_tushare'),
    'read_timeout': int(os.getenv('DB_READ_TIMEOUT_TUSHARE', '180')),
    'write_timeout': int(os.getenv('DB_WRITE_TIMEOUT_TUSHARE', '180')),
}


def _parsePercAnchors(raw: str | None) -> dict[str, str]:
    """解析 ``OPT_IV_PERC_ANCHORS='588000.SH=2024-09-24,510500.SH=2024-09-24'``。"""
    out: dict[str, str] = {}
    for item in str(raw or '').split(','):
        code, sep, day = item.partition('=')
        if not sep:
            continue
        code = code.strip()
        if code:
            out[code] = day.strip()
    return out


# 「Per C」自定义分位锚点：波动率中枢跃升后的起点，按标的配置（YYYY-MM-DD）。
# 留空 = 该标的无公认跃升点（大盘蓝筹中枢平稳，强行设锚点只会造出一列噪声），
# 此时 Per C 展示 — 并在 title 标明未配锚点。
# 2024-09-24 为成长/中小盘的中枢跃升日，已用 opt_iv_panel 月度中位数核对。
# 生效优先级：内置默认 < 环境变量 OPT_IV_PERC_ANCHORS < T 板页保存的 JSON。
OPT_IV_PERC_ANCHORS_DEFAULT: dict[str, str] = {
    '588000.SH': '2024-09-24',
    '159915.SZ': '2024-09-24',
    '510500.SH': '2024-09-24',
    '000852.SH': '2024-09-24',
    '510050.SH': '',
    '510300.SH': '',
    '000300.SH': '',
    '000016.SH': '',
}
OPT_IV_PERC_ANCHORS: dict[str, str] = dict(OPT_IV_PERC_ANCHORS_DEFAULT)
OPT_IV_PERC_ANCHORS.update(_parsePercAnchors(os.getenv('OPT_IV_PERC_ANCHORS')))


def appPath(*parts: str) -> str:
    return os.path.join(REPO_ROOT, *parts)


MA_SR_LEVELS_PATH = appPath('ma_sr_levels.json')
APP_LOCK_FILE = appPath('.rt_monitor_app.lock')
NIUSAN_HOLDER_CONFIG_PATH = appPath('niusan_holder_config.json')
# 财报页感兴趣股票标记（与浏览器无关，局域网多端共用）
FINA_INTEREST_MARKS_PATH = appPath('fina_interest_marks.json')
# T 板 Per C 锚点页面覆盖（勿提交个人日期）
OPT_IV_PERC_ANCHORS_PATH = appPath('opt_iv_perc_anchors.json')

# 代码更新时间：2026-10-04 21:46 RT_MONITOR_STATIC_VER 改为 static/+templates/ 内容哈希自动生成（环境变量可覆盖）；历史注释迁至 rt_monitor/CHANGELOG-static-ver.md
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
