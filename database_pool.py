#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库连接池管理模块
功能：提供高效的数据库连接池管理，支持连接复用和自动重连
作者：AI Assistant
版本：v1.0
日期：2025-01-27
"""

import os
import threading
import time
import logging
from typing import Optional, Dict, Any, List
from contextlib import contextmanager
import pymysql
from queue import Queue, Empty
# 配置参数将在主程序中定义

logger = logging.getLogger(__name__)


class ConnectionPool:
    """数据库连接池"""
    
    def __init__(self, db_config: Dict[str, Any], pool_size: int = 10, max_overflow: int = 20):
        self.db_config = db_config
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.max_connections = pool_size + max_overflow
        
        # 连接池
        self._pool = Queue(maxsize=self.pool_size)
        self._all_connections: List[pymysql.Connection] = []
        self._lock = threading.RLock()
        self._created_connections = 0
        
        # 统计信息
        self._stats = {
            'created': 0,
            'reused': 0,
            'closed': 0,
            'errors': 0,
            'peak_connections': 0
        }
        
        # 初始化连接池
        self._initialize_pool()
    
    def _initialize_pool(self):
        """初始化连接池"""
        try:
            logger.info(f"🔄 初始化数据库连接池，大小: {self.pool_size}")
            
            for _ in range(min(5, self.pool_size)):  # 预创建5个连接
                conn = self._create_connection()
                if conn:
                    self._pool.put(conn)
                    self._all_connections.append(conn)
            
            logger.info(f"✅ 连接池初始化完成，预创建连接: {len(self._all_connections)}")
            
        except Exception as e:
            logger.error(f"❌ 连接池初始化失败: {e}")
            raise
    
    def _create_connection(self) -> Optional[pymysql.Connection]:
        """创建新连接"""
        try:
            with self._lock:
                if self._created_connections >= self.max_connections:
                    logger.warning("⚠️ 已达到最大连接数限制")
                    return None
                
                conn = pymysql.connect(**self.db_config)
                self._created_connections += 1
                self._stats['created'] += 1
                self._stats['peak_connections'] = max(self._stats['peak_connections'], self._created_connections)
                
                logger.debug(f"🔗 创建新连接，当前连接数: {self._created_connections}")
                return conn
                
        except Exception as e:
            logger.error(f"❌ 创建连接失败: {e}")
            self._stats['errors'] += 1
            return None
    
    def _is_connection_alive(self, conn: pymysql.Connection) -> bool:
        """检查连接是否存活"""
        try:
            conn.ping(reconnect=False)
            return True
        except Exception:
            return False
    
    def get_connection(self, timeout: int = 30) -> Optional[pymysql.Connection]:
        """获取连接"""
        try:
            # 首先尝试从池中获取
            try:
                conn = self._pool.get(timeout=timeout)
                if self._is_connection_alive(conn):
                    self._stats['reused'] += 1
                    return conn
                else:
                    # 连接已断开，移除并创建新连接
                    self._close_connection(conn)
            except Empty:
                pass
            
            # 池中没有可用连接，创建新连接
            with self._lock:
                if self._created_connections < self.max_connections:
                    conn = self._create_connection()
                    if conn:
                        return conn
            
            logger.warning("⚠️ 无法获取数据库连接")
            return None
            
        except Exception as e:
            logger.error(f"❌ 获取连接失败: {e}")
            self._stats['errors'] += 1
            return None
    
    def return_connection(self, conn: pymysql.Connection):
        """归还连接"""
        try:
            if not conn:
                return
            
            # 检查连接是否有效
            if not self._is_connection_alive(conn):
                self._close_connection(conn)
                return
            
            # 回滚未提交的事务
            try:
                conn.rollback()
            except Exception:
                pass
            
            # 归还到池中
            try:
                self._pool.put_nowait(conn)
            except Exception:
                # 池已满，关闭连接
                self._close_connection(conn)
                
        except Exception as e:
            logger.error(f"❌ 归还连接失败: {e}")
            self._close_connection(conn)
    
    def _close_connection(self, conn: pymysql.Connection):
        """关闭连接"""
        try:
            if conn:
                conn.close()
                with self._lock:
                    if conn in self._all_connections:
                        self._all_connections.remove(conn)
                    self._created_connections -= 1
                    self._stats['closed'] += 1
                    
                logger.debug(f"🔒 关闭连接，当前连接数: {self._created_connections}")
                
        except Exception as e:
            logger.error(f"❌ 关闭连接失败: {e}")
    
    def close_all(self):
        """关闭所有连接"""
        try:
            with self._lock:
                # 关闭池中的所有连接
                while not self._pool.empty():
                    try:
                        conn = self._pool.get_nowait()
                        conn.close()
                        self._stats['closed'] += 1
                    except Empty:
                        break
                
                # 关闭所有连接
                for conn in self._all_connections[:]:
                    try:
                        conn.close()
                        self._stats['closed'] += 1
                    except Exception:
                        pass
                
                self._all_connections.clear()
                self._created_connections = 0
                
            logger.info("✅ 所有数据库连接已关闭")
            
        except Exception as e:
            logger.error(f"❌ 关闭连接池失败: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取连接池统计信息"""
        with self._lock:
            return {
                'pool_size': self.pool_size,
                'max_connections': self.max_connections,
                'current_connections': self._created_connections,
                'available_connections': self._pool.qsize(),
                'stats': self._stats.copy()
            }
    
    def health_check(self) -> bool:
        """健康检查"""
        try:
            conn = self.get_connection(timeout=5)
            if conn:
                self.return_connection(conn)
                return True
            return False
        except Exception:
            return False


class DatabasePoolManager:
    """数据库连接池管理器"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._pools: Dict[str, ConnectionPool] = {}
            self._initialized = True
    
    def get_pool(self, pool_name: str = 'default') -> ConnectionPool:
        """获取连接池"""
        if pool_name not in self._pools:
            with self._lock:
                if pool_name not in self._pools:
                    if pool_name == 'default':
                        # 这些配置将在主程序中设置，如果没有设置则使用默认值
                        _fallbackPwd = os.getenv('DB_PASSWORD', '').strip()
                        if not _fallbackPwd:
                            raise SystemExit('缺少环境变量 DB_PASSWORD')
                        db_config = getattr(self, '_default_db_config', {
                            'host': 'localhost',
                            'port': 3306,
                            'user': 'root',
                            'password': os.getenv("DB_PASSWORD", "").strip(),
                            'database': 'stock_wind',
                            'charset': 'utf8mb4',
                            'autocommit': False,
                            'connect_timeout': 7200,
                            'read_timeout': 7200,
                            'write_timeout': 7200,
                            'init_command': "SET SESSION wait_timeout=28800, interactive_timeout=28800, innodb_lock_wait_timeout=7200, lock_wait_timeout=7200",
                            'sql_mode': 'TRADITIONAL'
                        })
                        pool_size = getattr(self, '_default_pool_size', 20)  # 🔥 恢复2025-10-03 23:42:48稳定版本：20个基础连接
                        max_overflow = getattr(self, '_default_max_overflow', 50)  # 🔥 恢复2025-10-03 23:42:48稳定版本：50个溢出连接
                    else:
                        # 其他数据库配置
                        raise ValueError(f"未知的连接池名称: {pool_name}")
                    
                    self._pools[pool_name] = ConnectionPool(
                        db_config=db_config,
                        pool_size=pool_size,
                        max_overflow=max_overflow
                    )
        
        return self._pools[pool_name]
    
    def close_all_pools(self):
        """关闭所有连接池"""
        for pool in self._pools.values():
            pool.close_all()
        self._pools.clear()
    
    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有连接池统计信息"""
        return {name: pool.get_stats() for name, pool in self._pools.items()}


# 全局连接池管理器
pool_manager = DatabasePoolManager()


@contextmanager
def get_db_connection(pool_name: str = 'default', timeout: int = 30):
    """
    数据库连接上下文管理器
    
    用法:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM table")
            result = cursor.fetchall()
    """
    pool = pool_manager.get_pool(pool_name)
    conn = None
    
    try:
        conn = pool.get_connection(timeout)
        if not conn:
            raise ConnectionError("无法获取数据库连接")
        
        yield conn
        
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise e
        
    finally:
        if conn:
            pool.return_connection(conn)


def retry_with_backoff(func, *args, **kwargs):
    """
    带指数退避的重试装饰器（激进优化版）
    """
    # 🔥 恢复2025-10-03 23:42:48稳定版本：平衡的重试配置
    max_retry_times = 3  # 稳定版本：3次重试
    retry_delay = 0.2  # 稳定版本：0.2秒延迟
    max_retry_delay = 3  # 稳定版本：3秒最大延迟
    
    for attempt in range(max_retry_times):
        # 检查中断信号
        try:
            # 尝试导入全局中断变量
            import sys
            if hasattr(sys.modules.get('__main__', None), 'interrupted') and sys.modules['__main__'].interrupted:
                logger.info("⚠️ 检测到中断信号，停止数据库重试")
                raise KeyboardInterrupt("用户中断")
        except:
            pass  # 如果无法检查中断信号，继续执行
        
        try:
            return func(*args, **kwargs)
        except (pymysql.Error, ConnectionError, TimeoutError) as e:
            error_code = getattr(e, 'args', [0])[0] if hasattr(e, 'args') and e.args else 0
            
            # 🔥 智能错误处理：死锁和锁超时快速失败
            if error_code in [1213, 1205]:  # 死锁或锁等待超时
                if attempt >= 2:  # 死锁重试2次后直接失败
                    logger.error(f"❌ 死锁/锁超时，快速失败: {e}")
                    raise e
                delay = 0.1  # 死锁快速重试
            else:
                delay = min(retry_delay * (2 ** attempt), max_retry_delay)
            
            if attempt == max_retry_times - 1:
                logger.error(f"❌ 数据库操作最终失败，已重试{max_retry_times}次")
                raise e
            
            logger.warning(f"⚠️ 数据库操作失败，{delay:.1f}秒后重试 (第{attempt + 1}/{max_retry_times}次)")
            time.sleep(delay)
        except Exception as e:
            # 非数据库相关错误直接抛出
            raise e


def health_check() -> Dict[str, bool]:
    """健康检查所有连接池"""
    stats = pool_manager.get_all_stats()
    health_status = {}
    
    for pool_name, pool_stats in stats.items():
        pool = pool_manager.get_pool(pool_name)
        health_status[pool_name] = pool.health_check()
    
    return health_status
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
