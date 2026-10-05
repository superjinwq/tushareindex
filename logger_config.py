#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志配置模块
功能：提供统一的日志管理，支持结构化日志和性能优化
作者：AI Assistant
版本：v1.0
日期：2025-01-27
"""

import os
import sys
import json
import logging
import logging.handlers
import time
from datetime import datetime
from typing import Dict, Any, Optional
# 配置已整合到主程序中


class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器"""
    
    def __init__(self, include_extra_fields: bool = True):
        super().__init__()
        self.include_extra_fields = include_extra_fields
    
    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        # 基础字段
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno
        }
        
        # 添加额外字段
        if self.include_extra_fields:
            extra_fields = {}
            for key, value in record.__dict__.items():
                if key not in ['name', 'msg', 'args', 'levelname', 'levelno', 'pathname',
                              'filename', 'module', 'exc_info', 'exc_text', 'stack_info',
                              'lineno', 'funcName', 'created', 'msecs', 'relativeCreated',
                              'thread', 'threadName', 'processName', 'process', 'getMessage']:
                    extra_fields[key] = value
            
            if extra_fields:
                log_data['extra'] = extra_fields
        
        # 添加异常信息
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        # 添加性能信息
        if hasattr(record, 'duration'):
            log_data['duration'] = record.duration
        
        if hasattr(record, 'records_processed'):
            log_data['records_processed'] = record.records_processed
        
        return json.dumps(log_data, ensure_ascii=False, default=str)


class PerformanceFilter(logging.Filter):
    """性能日志过滤器"""
    
    def __init__(self, min_duration: float = 1.0):
        super().__init__()
        self.min_duration = min_duration
    
    def filter(self, record: logging.LogRecord) -> bool:
        """过滤性能相关的日志"""
        if hasattr(record, 'duration'):
            return record.duration >= self.min_duration
        return True


class DatabaseOperationFilter(logging.Filter):
    """数据库操作日志过滤器"""
    
    def __init__(self, log_sql: bool = False):
        super().__init__()
        self.log_sql = log_sql
    
    def filter(self, record: logging.LogRecord) -> bool:
        """过滤数据库操作日志"""
        if hasattr(record, 'sql') and not self.log_sql:
            return False
        return True


class LoggerConfig:
    """日志配置管理器"""
    
    def __init__(self):
        # 使用默认日志配置
        self.log_config = type('LogConfig', (), {
            'log_dir': 'logs',
            'level': 'INFO',
            'enable_file': True,
            'enable_console': True,
            'max_size': 10 * 1024 * 1024,  # 10MB
            'backup_count': 5,
            'log_format': '%(asctime)s - %(levelname)s - %(message)s',
            'date_format': '%Y-%m-%d %H:%M:%S'
        })()
        self._setup_logging()
    
    def _setup_logging(self):
        """设置日志配置"""
        # pytest 等场景设 LOGGER_CONFIG_NO_FILE=1：只留控制台，不写 logs/ 正式日志
        if os.getenv('LOGGER_CONFIG_NO_FILE') == '1':
            self.log_config.enable_file = False
        # 创建日志目录
        if not os.path.exists(self.log_config.log_dir):
            os.makedirs(self.log_config.log_dir)
        
        # 配置根日志器
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, self.log_config.level))
        
        # 清除现有处理器
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 添加文件处理器
        if self.log_config.enable_file:
            self._add_file_handlers(root_logger)
        
        # 添加控制台处理器
        if self.log_config.enable_console:
            self._add_console_handler(root_logger)
        
        # 配置特定模块的日志级别
        self._configure_module_loggers()
    
    def _add_file_handlers(self, logger: logging.Logger):
        """添加文件处理器"""
        # 主日志文件（带轮转）
        main_handler = logging.handlers.RotatingFileHandler(
            os.path.join(self.log_config.log_dir, '融资融券明细数据导入.log'),
            maxBytes=self.log_config.max_size,
            backupCount=self.log_config.backup_count,
            encoding='utf-8'
        )
        main_handler.setFormatter(logging.Formatter(
            self.log_config.log_format,
            datefmt=self.log_config.date_format
        ))
        logger.addHandler(main_handler)
        
        # 结构化日志文件（JSON格式）
        structured_handler = logging.handlers.RotatingFileHandler(
            os.path.join(self.log_config.log_dir, 'structured.log'),
            maxBytes=self.log_config.max_size,
            backupCount=self.log_config.backup_count,
            encoding='utf-8'
        )
        structured_handler.setFormatter(StructuredFormatter())
        logger.addHandler(structured_handler)
        
        # 性能日志文件
        performance_handler = logging.handlers.RotatingFileHandler(
            os.path.join(self.log_config.log_dir, 'performance.log'),
            maxBytes=self.log_config.max_size,
            backupCount=self.log_config.backup_count,
            encoding='utf-8'
        )
        performance_handler.setFormatter(StructuredFormatter())
        performance_handler.addFilter(PerformanceFilter(min_duration=0.5))
        logger.addHandler(performance_handler)
        
        # 错误日志文件
        error_handler = logging.handlers.RotatingFileHandler(
            os.path.join(self.log_config.log_dir, 'error.log'),
            maxBytes=self.log_config.max_size,
            backupCount=self.log_config.backup_count,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(StructuredFormatter())
        logger.addHandler(error_handler)
    
    def _add_console_handler(self, logger: logging.Logger):
        """添加控制台处理器"""
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        ))
        
        # 添加颜色支持
        if sys.platform != 'win32':
            console_handler.addFilter(ColorFilter())
        
        logger.addHandler(console_handler)
    
    def _configure_module_loggers(self):
        """配置特定模块的日志级别"""
        # 第三方库日志级别
        logging.getLogger('pymysql').setLevel(logging.WARNING)
        logging.getLogger('tushare').setLevel(logging.WARNING)
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        
        # 应用模块日志级别
        logging.getLogger('database_pool').setLevel(logging.INFO)
        logging.getLogger('performance_monitor').setLevel(logging.INFO)
    
    def get_logger(self, name: str) -> logging.Logger:
        """获取日志器"""
        return logging.getLogger(name)


class ColorFilter(logging.Filter):
    """颜色过滤器（Unix系统）"""
    
    COLORS = {
        'DEBUG': '\033[36m',    # 青色
        'INFO': '\033[32m',     # 绿色
        'WARNING': '\033[33m',  # 黄色
        'ERROR': '\033[31m',    # 红色
        'CRITICAL': '\033[35m', # 紫色
        'RESET': '\033[0m'      # 重置
    }
    
    def filter(self, record: logging.LogRecord) -> bool:
        """添加颜色到日志记录"""
        if record.levelname in self.COLORS:
            record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{self.COLORS['RESET']}"
        return True


class LoggerMixin:
    """日志器混入类"""
    
    @property
    def logger(self) -> logging.Logger:
        """获取日志器"""
        if not hasattr(self, '_logger'):
            self._logger = logging.getLogger(self.__class__.__name__)
        return self._logger
    
    def log_performance(self, operation: str, duration: float, records: int = 0, **kwargs):
        """记录性能日志"""
        extra = {
            'operation': operation,
            'duration': duration,
            'records_processed': records,
            **kwargs
        }
        self.logger.info(f"⏱️ {operation} 完成，耗时: {duration:.2f}s，记录数: {records}", extra=extra)
    
    def log_database_operation(self, operation: str, table: str, affected_rows: int, duration: float = 0):
        """记录数据库操作日志"""
        extra = {
            'operation': operation,
            'table': table,
            'affected_rows': affected_rows,
            'duration': duration
        }
        self.logger.info(f"🗄️ {operation} {table}，影响行数: {affected_rows}", extra=extra)
    
    def log_api_call(self, api_name: str, success: bool, duration: float = 0, records: int = 0, error: str = None):
        """记录API调用日志"""
        extra = {
            'api_name': api_name,
            'success': success,
            'duration': duration,
            'records': records
        }
        if error:
            extra['error'] = error
        
        status = "✅" if success else "❌"
        if success:
            self.logger.info(f"📡 {status} {api_name}，耗时: {duration:.2f}s，记录数: {records}", extra=extra)
        else:
            self.logger.error(f"📡 {status} {api_name}，耗时: {duration:.2f}s，记录数: {records}，错误: {error}", extra=extra)


def setup_logging():
    """设置日志配置"""
    return LoggerConfig()


from windows_safe_log import WindowsSafeTimedRotatingFileHandler


def useDedicatedLogFile(logName: str, backupCount: int = 3) -> str:
    """可选入口：把根日志器改为「logs/<logName>.log 单文件按天轮转 + 控制台」。

    仅供常驻服务入口（如 rt_monitor_app）显式调用；未调用的脚本保持模块导入时的默认配置。
    返回日志文件绝对路径。
    """
    logDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(logDir, exist_ok=True)
    logPath = os.path.join(logDir, f'{logName}.log')
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    if os.getenv('LOGGER_CONFIG_NO_FILE') != '1':
        fileHandler = WindowsSafeTimedRotatingFileHandler(
            logPath, when='midnight', interval=1, backupCount=backupCount, encoding='utf-8'
        )
        fileHandler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
        ))
        root.addHandler(fileHandler)
    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
    root.addHandler(consoleHandler)
    root.setLevel(logging.INFO)
    return logPath


def get_logger(name: str) -> logging.Logger:
    """获取日志器"""
    return logging.getLogger(name)


def log_startup_info():
    """记录启动信息"""
    logger = get_logger(__name__)
    logger.info("🚀 程序启动")
    logger.info(f"📅 启动时间: {datetime.now().isoformat()}")
    logger.info(f"🐍 Python版本: {sys.version}")
    logger.info(f"📁 工作目录: {os.getcwd()}")
    logger.info(f"🔧 日志级别: INFO")


def log_shutdown_info():
    """记录关闭信息"""
    logger = get_logger(__name__)
    logger.info("🛑 程序关闭")
    logger.info(f"📅 关闭时间: {datetime.now().isoformat()}")


# 初始化日志配置
logger_config = setup_logging()
# 代码更新时间：2026-10-04 17:42 可选 useDedicatedLogFile；LOGGER_CONFIG_NO_FILE 关文件日志
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
