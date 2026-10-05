"""pytest 全局：测试进程不写、不轮转仓库内任何正式日志。

opt_mins / fut_mapping / fut_trade_cal / fina_indicator / opt_vertical_spread_backtest 已在源头修复
（宿主已有 handler 即不配置，见 tests/test_import_time_logging.py）；tusharert 等仍在 import 时自带具名文件 Handler，
这里保留 Handler 层兜底拦截：目标文件位于仓库目录内则丢弃该条记录（tmp_path 等仓库外路径照常写）。
"""
import logging
import logging.handlers
import os

os.environ['LOGGER_CONFIG_NO_FILE'] = '1'

_REPO_PREFIX = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).lower() + os.sep


def _isRepoLog(handler) -> bool:
    return os.path.abspath(getattr(handler, 'baseFilename', '')).lower().startswith(_REPO_PREFIX)


def _guard(origEmit):
    def emit(self, record):
        if _isRepoLog(self):
            return
        origEmit(self, record)
    return emit


logging.FileHandler.emit = _guard(logging.FileHandler.emit)
logging.handlers.BaseRotatingHandler.emit = _guard(logging.handlers.BaseRotatingHandler.emit)

# 代码更新时间：2026-10-04 17:42 pytest 不写正式日志
# 代码更新时间：2026-10-04 21:04 opt_mins 等已源头修复，保留兜底拦截
