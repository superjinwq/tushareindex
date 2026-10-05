# 导入必要的库
import threading
from concurrent.futures import ThreadPoolExecutor, wait
import mysql.connector
import tushare as ts
import pandas as pd
import time
import logging
from logging.handlers import TimedRotatingFileHandler
import signal
import errno
import sys
from datetime import datetime
from collections import deque
from decimal import ROUND_HALF_UP, Decimal
import json
import os
import re
import statistics
import atexit

# 配置日志 - 按天轮转，保留3天；Windows 下轮转失败时安全降级，避免 PermissionError
log_filename = 'tusharert个股实时行情.log'
log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')


from windows_safe_log import ManualRenameTimedRotatingFileHandler as WindowsSafeTimedRotatingFileHandler


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 控制台输出
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_format)
logger.addHandler(console_handler)

# 文件输出 - 按天轮转，保留3天；使用 Windows 安全轮转，避免「另一程序正在使用此文件」
file_handler = WindowsSafeTimedRotatingFileHandler(
    log_filename, when='midnight', interval=1, backupCount=3, encoding='utf-8'
)
file_handler.setFormatter(log_format)
file_handler.suffix = "%Y%m%d"
logger.addHandler(file_handler)

# 全局变量
memory_cache = pd.DataFrame(columns=['ts_code', 'name', 'pre_close', 'high', 'open', 'low', 'close', 
                                     'vol', 'amount', 'num', 'ask_price1', 'ask_volume1', 'bid_price1', 'bid_volume1',
                                     'trade_time'])
memory_cache.set_index('ts_code', inplace=True)

cache_lock = threading.Lock()
running = True
stats_lock = threading.Lock()
_stop_logged = False

stats = {
    'start_time': None,
    'data_updates': 0,
    'db_writes': 0,
    'last_update_time': None,
    'tushare_api_calls': 0,
    'errors': 0,
    'rate_limit_hits': 0,
    'tushare_idx_api_calls': 0,
    'idx_db_writes': 0,
    'tushare_sw_api_calls': 0,
    'sw_db_writes': 0,
    'qmt_ticks': 0,
    'qmt_writes': 0,
    'qmt_fallback': 0,
}

# QMT 全推 PUB：只 SUB，不再 subscribe_whole_quote。RT_K_QMT_PRIMARY=0 可立刻退回纯 Tushare。
def _envFlagOn(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    return default


RT_K_QMT_PRIMARY = _envFlagOn("RT_K_QMT_PRIMARY", True)
QMT_PUSH_ADDR = os.getenv("QMT_QUOTE_PUSH_ADDR", "tcp://127.0.0.1:15574")
XTQUANT_SRC = os.getenv("XTQUANT_SRC", r"D:\cursor\xtquant_big_convert\src")
QMT_FLUSH_SEC = float(os.getenv("QMT_FLUSH_SEC", "1.5"))
QMT_SILENCE_FALLBACK_SEC = float(os.getenv("QMT_SILENCE_FALLBACK_SEC", "8"))
QMT_UNIQUE_MIN = int(os.getenv("QMT_UNIQUE_MIN", "4800"))
QMT_WARMUP_SEC = float(os.getenv("QMT_WARMUP_SEC", "45"))
QMT_SANITY_N = 20
QMT_SANITY_RATIO_LO = 0.92
QMT_SANITY_RATIO_HI = 1.08
INSTANCE_LOCK_NAME = "tusharert_rt_k.lock"
INSTANCE_MUTEX_NAME = "Local\\tusharert_rt_k"
_instanceLockFile = None
_instanceLockFiles = []
_instanceMutexHandle = None
_qmtStop = threading.Event()
qmtLock = threading.Lock()
qmtState = {
    "enabled": RT_K_QMT_PRIMARY,
    "lastPushMono": 0.0,
    "startedMono": 0.0,
    "uniqueCodes": set(),
    "dirty": {},
    "allowSet": set(),
    "paused": True,
    "sanityOk": None,
    "sanityRatios": [],
    "fallbackReason": "init",
}

# 与 rt_k_代码分类.py A 股号段一致（含 302）；ETF/B 股/转债不进 QMT 写库。
A_SHARE_TS_CODE_RE = re.compile(
    r"^(?:"
    r"(?:600|601|603|605|688|689)\d{3}\.SH|"
    r"(?:000|001|002|003|300|301|302)\d{3}\.SZ|"
    r"[48]\d{5}\.BJ|"
    r"920\d{3}\.BJ"
    r")$"
)

if not os.getenv("DB_PASSWORD", "").strip():
    raise SystemExit("缺少环境变量 DB_PASSWORD")
# 数据库配置 - stock_wind（rt_k 个股/基金实时）
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': os.getenv("DB_PASSWORD", "").strip(),
    'database': 'stock_wind',
    'charset': 'utf8mb4',
    'autocommit': False,
    'pool_name': 'tushare_pool',
    'pool_size': 5,
    'pool_reset_session': True,
    'connect_timeout': 10,
    'read_timeout': 30,
    'write_timeout': 30
}

# 数据库配置 - stock_tushare（rt_idx_k / rt_sw_k，独立限流 50 次/分钟）
DB_CONFIG_TUSHARE = {
    'host': 'localhost',
    'user': 'root',
    'password': os.getenv("DB_PASSWORD", "").strip(),
    'database': 'stock_tushare',
    'charset': 'utf8mb4',
    'autocommit': False,
    'pool_name': 'tushare_idx_pool',
    'pool_size': 2,
    'pool_reset_session': True,
    'connect_timeout': 10,
    'read_timeout': 30,
    'write_timeout': 30
}

# Tushare配置
# 限频 50 次/分钟 → 相邻两次从开始发请求起至少 1.2s；撞限频后按档加间隔，连续成功后再收回
if not os.getenv("TUSHARE_TOKEN", "").strip():
    raise SystemExit("缺少环境变量 TUSHARE_TOKEN")
TUSHARE_CONFIG = {
    'token': os.getenv("TUSHARE_TOKEN", "").strip(),
    'max_calls_per_minute': 50,
    'inter_call_interval': 1.2,       # 基准：开始起算，60/50
    'inter_call_interval_max': 1.5,   # 自适应上限（约 40 次/分）
    'inter_call_interval_step': 0.05, # 每次限频 +0.05s，收回同幅
    'inter_call_recover_ok': 50,      # 连续成功约满一窗后再收一档
    'polling_interval': 0,            # 单轮节奏已由间隔保证
}

# Buffer Pool优化配置 - 专门为Tushare实时行情分配500MB
BUFFER_POOL_CONFIG = {
    'target_size_mb': 500,               # 目标Buffer Pool大小：500MB
    'monitor_interval': 300,             # 监控间隔：5分钟
    'performance_threshold': 0.90,       # 性能阈值：90%命中率
    'optimization_enabled': True,        # 启用自动优化
    'memory_ratio': 0.00195,            # 500MB/256GB = 0.195%
    'max_connections': 5,                # 最大连接数
    'table_cache_size': 2000,           # 表缓存大小
    'query_cache_size': 0               # 禁用查询缓存（MySQL 8.0+）
}

# 股票代码配置 - 简化版本
STOCK_PATTERNS = [
    "*.SH",     # 上海市场
    "*.SZ",     # 深圳市场
    "*.BJ"      # 北交所市场
]

# 特殊主题：rt_k + HQ_FND_TICK 拉基金/ETF。*.SZ 无 topic 会带上 159xxx 但冻在旧交易日，
# 页面按当日过滤即显示 --；深市必须单独走基金主题（与沪市对称）。
SPECIAL_TOPICS = [
    {
        'ts_code': '*.SH',
        'topic': 'HQ_FND_TICK',
        'name': '上海基金主题数据'
    },
    {
        'ts_code': '*.SZ',
        'topic': 'HQ_FND_TICK',
        'name': '深圳基金主题数据'
    },
]

# 交易所指数实时日线 rt_idx_k（独立权限 50 次/分钟，与 rt_k 分开限流）
ENABLE_RT_IDX_K = True
RT_IDX_K_TABLE = 'rt_idx_k'
IDX_PATTERNS = ['0*.SH', '3*.SZ']
IDX_FIELDS = 'ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount'

# 申万行业指数实时截面 rt_sw_k（单独权限；全量一次约 180 条，与 rt_idx_k 共用 stock_tushare 池）
# 与根目录 rt_sw_k.py 二选一常驻，避免双进程重复请求同一接口。
ENABLE_RT_SW_K = True
RT_SW_K_TABLE = 'rt_sw_k'
RT_SW_K_FIELDS = (
    'ts_code,name,trade_time,close,pre_close,high,open,low,vol,amount,pct_change'
)

# 字段列表
FIELDS_LIST = [
    "ts_code", "name", "pre_close", "high", "open", "low", "close",
    "vol", "amount", "num", "ask_price1", "ask_volume1", "bid_price1", "bid_volume1",
    "trade_time"
]

# rt_k 数值列统一为 float64，避免 memory_cache.update 时 int64 与 float64 混用触发 pandas 2.x 报错
RT_K_FLOAT_COLS = [
    'pre_close', 'high', 'open', 'low', 'close', 'vol', 'amount', 'num',
    'ask_price1', 'ask_volume1', 'bid_price1', 'bid_volume1',
]


def ensure_rt_k_float_dtypes(data_df: pd.DataFrame) -> pd.DataFrame:
    """将行情数值列统一为 float64（pd.to_numeric 在无 NaN 时可能保留 int64）。"""
    if data_df is None or data_df.empty:
        return data_df
    out = data_df
    for col in RT_K_FLOAT_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce').astype('float64')
    return out


# 数据验证配置
DATA_VALIDATION_CONFIG = {
    'price_max': 999999.99,      # 价格字段最大值
    'price_min': 0,              # 价格字段最小值（停牌/集合竞价前合法值为0）
    'volume_max': 999999999999,  # 成交量最大值
    'volume_min': 0,             # 成交量最小值
    'amount_max': 999999999999999,  # 成交额最大值
    'amount_min': 0,              # 成交额最小值
    'name_max_length': 50        # 股票名称最大长度
}

def validate_and_clean_data(data_df):
    """验证和清理数据，确保数值在合理范围内"""
    if data_df is None or data_df.empty:
        return data_df
    
    cleaned_df = data_df.copy()
    
    # 价格字段验证和清理
    price_fields = ['pre_close', 'high', 'open', 'low', 'close', 'ask_price1', 'bid_price1']
    for field in price_fields:
        if field in cleaned_df.columns:
            # 转换为数值类型
            cleaned_df[field] = pd.to_numeric(cleaned_df[field], errors='coerce')
            # 替换异常值
            cleaned_df[field] = cleaned_df[field].fillna(0)
            # 限制范围
            cleaned_df[field] = cleaned_df[field].clip(
                lower=DATA_VALIDATION_CONFIG['price_min'],
                upper=DATA_VALIDATION_CONFIG['price_max']
            )
    
    # 成交量字段验证和清理
    volume_fields = ['vol', 'ask_volume1', 'bid_volume1']
    for field in volume_fields:
        if field in cleaned_df.columns:
            cleaned_df[field] = pd.to_numeric(cleaned_df[field], errors='coerce')
            cleaned_df[field] = cleaned_df[field].fillna(0)
            cleaned_df[field] = cleaned_df[field].clip(
                lower=DATA_VALIDATION_CONFIG['volume_min'],
                upper=DATA_VALIDATION_CONFIG['volume_max']
            )
    
    # 成交笔数字段验证和清理
    if 'num' in cleaned_df.columns:
        cleaned_df['num'] = pd.to_numeric(cleaned_df['num'], errors='coerce')
        cleaned_df['num'] = cleaned_df['num'].fillna(0)
    
    # 成交额字段验证和清理
    if 'amount' in cleaned_df.columns:
        cleaned_df['amount'] = pd.to_numeric(cleaned_df['amount'], errors='coerce')
        cleaned_df['amount'] = cleaned_df['amount'].fillna(0)
        cleaned_df['amount'] = cleaned_df['amount'].clip(
            lower=DATA_VALIDATION_CONFIG['amount_min'],
            upper=DATA_VALIDATION_CONFIG['amount_max']
        )
    
    # 股票名称字段验证和清理
    if 'name' in cleaned_df.columns:
        cleaned_df['name'] = cleaned_df['name'].fillna('')
        # 限制长度并清理特殊字符（保留*，因*ST为退市风险警示名称的一部分）
        cleaned_df['name'] = cleaned_df['name'].astype(str).str[:DATA_VALIDATION_CONFIG['name_max_length']]
        # 使用显式 Unicode 字符范围（一=U+4E00, 龯=U+9FFF），避免 \u 转义在 re 中报错
        cleaned_df['name'] = cleaned_df['name'].str.replace(r'[^\w\s一-龯*]', '', regex=True)
    
    # 股票代码字段验证
    if 'ts_code' in cleaned_df.columns:
        cleaned_df['ts_code'] = cleaned_df['ts_code'].fillna('')
        # 确保股票代码格式正确
        cleaned_df['ts_code'] = cleaned_df['ts_code'].astype(str).str.strip()
    
    # 行情时间字段验证和清理
    if 'trade_time' in cleaned_df.columns:
        # 转换为datetime类型，非法值转为NaT
        cleaned_df['trade_time'] = pd.to_datetime(cleaned_df['trade_time'], errors='coerce')
        # NaT保持为NaT，入库时会转为NULL
    
    return ensure_rt_k_float_dtypes(cleaned_df)

def signal_handler(signum, frame):
    """信号处理器 - 确保用户能够正常中断程序"""
    global running
    global _stop_logged
    running = False
    if not _stop_logged:
        logger.info(f"接收到终止信号 {signum}，开始优雅退出...")
        _stop_logged = True

# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ======================= 轮询时间窗口（放宽） =======================
# 仅在此窗口内调用 Tushare 实时接口（rt_k / rt_idx_k / rt_sw_k），窗口外仅做时间检查并休眠。
POLLING_WINDOW_START = "09:10:00"
POLLING_WINDOW_END = "15:10:00"
SLEEP_NON_TRADING_SEC = 60


def _is_polling_time_910_1510() -> bool:
    """
    是否处于轮询时间窗口 9:10～15:10（含，按本地时间）。
    较实际交易 9:30～15:00 放宽：9:10 便于开盘前就绪，15:10 便于拿到 15:00 收盘 bar。
    """
    now_t = datetime.now().time()
    start_t = datetime.strptime(POLLING_WINDOW_START, "%H:%M:%S").time()
    end_t = datetime.strptime(POLLING_WINDOW_END, "%H:%M:%S").time()
    return start_t <= now_t <= end_t


def _sleep_interruptible(total_sec: float) -> None:
    """可中断睡眠：按 0.5s 分段 sleep，便于 Ctrl+C 秒退。
    remain 先算再 sleep，避免 while 与 sleep 之间时钟前移导致 ValueError。"""
    end_ts = time.monotonic() + max(0.0, float(total_sec))
    while running:
        remain = end_ts - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(0.5, remain))


# 简化的时间窗口管理器
class SimpleTimeManager:
    def __init__(
        self,
        max_calls=50,
        window_size=60,
        baseInterval=1.2,
        maxInterval=1.5,
        intervalStep=0.05,
        recoverOk=50,
    ):
        self.max_calls = max_calls
        self.window_size = window_size
        self.call_times = deque()
        self.lock = threading.Lock()
        self.baseInterval = float(baseInterval)
        self.maxInterval = float(maxInterval)
        self.intervalStep = float(intervalStep)
        self.recoverOk = int(recoverOk)
        self.interval = self.baseInterval
        self._okStreak = 0

    def currentInterval(self) -> float:
        with self.lock:
            return self.interval

    def widenOnLimit(self) -> float:
        """服务端限频：开始间隔加一档，连续成功计数清零。"""
        with self.lock:
            self._okStreak = 0
            self.interval = min(self.maxInterval, round(self.interval + self.intervalStep, 3))
            return self.interval

    def noteCallOk(self) -> float:
        """请求被服务端接受：连续成功满一窗则间隔收回一档，直到基准 1.2s。"""
        with self.lock:
            if self.interval <= self.baseInterval + 1e-9:
                self._okStreak = 0
                return self.interval
            self._okStreak += 1
            if self._okStreak < self.recoverOk:
                return self.interval
            self._okStreak = 0
            old = self.interval
            self.interval = max(self.baseInterval, round(self.interval - self.intervalStep, 3))
            new = self.interval
        if new < old:
            logger.info("限频开始间隔回落 %.2f -> %.2fs", old, new)
        return new
    
    def can_make_call(self):
        """检查是否可以调用API"""
        with self.lock:
            current_time = time.time()
            # 清理过期的调用记录
            while self.call_times and current_time - self.call_times[0] > self.window_size:
                self.call_times.popleft()
            return len(self.call_times) < self.max_calls
    
    def record_call(self):
        """记录API调用"""
        with self.lock:
            self.call_times.append(time.time())
    
    def get_current_calls(self):
        """获取当前时间窗口内的调用次数"""
        with self.lock:
            current_time = time.time()
            while self.call_times and current_time - self.call_times[0] > self.window_size:
                self.call_times.popleft()
            return len(self.call_times)

    def time_until_next_call_allowed(self) -> float:
        """
        距离下一次允许调用还需等待的秒数（基于滑动窗口 max_calls/window_size）。
        返回 0 表示现在即可调用。
        """
        with self.lock:
            current_time = time.time()
            while self.call_times and current_time - self.call_times[0] > self.window_size:
                self.call_times.popleft()
            if len(self.call_times) < self.max_calls:
                return 0.0
            if not self.call_times:
                return 0.0
            oldest = self.call_times[0]
            return max(0.0, self.window_size - (current_time - oldest))

    def markServerLimited(self) -> float:
        """服务端已拒：本地窗口可能几乎空（双实例抢同一 token），按打满处理。"""
        with self.lock:
            now = time.time()
            while self.call_times and now - self.call_times[0] > self.window_size:
                self.call_times.popleft()
            localN = len(self.call_times)
            if localN < self.max_calls:
                self.call_times.clear()
                for _ in range(self.max_calls):
                    self.call_times.append(now)
                return float(self.window_size)
            oldest = self.call_times[0]
            return max(0.0, self.window_size - (now - oldest))


def isTushareFreqLimited(err) -> bool:
    """识别 rt_k/rt_idx_k/rt_sw_k 服务端限频。实际返回「频率超限(50次/分钟)」，文档旧句不同。"""
    text = str(err or "")
    return any(
        mark in text
        for mark in ("频率超限", "50次/分钟", "50次/分", "每分钟最多访问")
    )


def _noteFreqLimited(mgr: SimpleTimeManager, apiName: str, where: str, err) -> float:
    update_stats("rate_limit_hits")
    waitSec = mgr.markServerLimited()
    newIv = mgr.widenOnLimit()
    logger.warning(
        "检测到 %s 频率超限（%s），本轮停止后续请求，窗口冷却 %.1fs，开始间隔调至 %.2fs: %s",
        apiName, where, waitSec, newIv, err,
    )
    return waitSec


def _waitUntilApiSlot(mgr: SimpleTimeManager, apiName: str) -> bool:
    """有空位则打当前这次。窗口仍满时最多短等一档间隔，避免 60s 冷却堵住并行的另外两套接口。"""
    if not running:
        return False
    if mgr.can_make_call():
        return True
    remain = mgr.time_until_next_call_allowed()
    cap = max(mgr.currentInterval(), 0.5)
    sliceSec = min(max(remain, 0.05), cap)
    logger.warning(
        "%s 窗口暂满（约 %.1fs 后有空位），本轮让路 %.1fs，不堵并行接口",
        apiName, remain, sliceSec,
    )
    _sleep_interruptible(sliceSec)
    return bool(running and mgr.can_make_call())

# Buffer Pool性能监控器
class BufferPoolMonitor:
    def __init__(self, connection_pool, config):
        self.connection_pool = connection_pool
        self.config = config
        self.last_check = time.time()
        self.performance_history = []
        self.lock = threading.Lock()
    
    def get_buffer_pool_status(self):
        """获取Buffer Pool状态"""
        try:
            connection = self.connection_pool.get_connection()
            cursor = connection.cursor()
            
            # 获取Buffer Pool大小
            cursor.execute("SHOW VARIABLES LIKE 'innodb_buffer_pool_size'")
            size_result = cursor.fetchone()
            buffer_pool_size_mb = 0
            if size_result:
                size_bytes = int(size_result[1])
                buffer_pool_size_mb = size_bytes / (1024**2)
            
            # 获取Buffer Pool使用情况
            cursor.execute("SHOW STATUS LIKE 'Innodb_buffer_pool_pages_total'")
            total_pages = cursor.fetchone()
            cursor.execute("SHOW STATUS LIKE 'Innodb_buffer_pool_pages_free'")
            free_pages = cursor.fetchone()
            cursor.execute("SHOW STATUS LIKE 'Innodb_buffer_pool_pages_data'")
            data_pages = cursor.fetchone()
            
            # 获取命中率
            cursor.execute("SHOW STATUS LIKE 'Innodb_buffer_pool_read_requests'")
            read_requests = cursor.fetchone()
            cursor.execute("SHOW STATUS LIKE 'Innodb_buffer_pool_reads'")
            disk_reads = cursor.fetchone()
            
            cursor.close()
            connection.close()
            
            # 计算指标
            total = int(total_pages[1]) if total_pages else 0
            free = int(free_pages[1]) if free_pages else 0
            data = int(data_pages[1]) if data_pages else 0
            used_percent = ((total - free) / total * 100) if total > 0 else 0
            
            requests = int(read_requests[1]) if read_requests else 0
            reads = int(disk_reads[1]) if disk_reads else 0
            hit_rate = ((requests - reads) / requests * 100) if requests > 0 else 0
            
            return {
                'buffer_pool_size_mb': buffer_pool_size_mb,
                'total_pages': total,
                'free_pages': free,
                'data_pages': data,
                'used_percent': used_percent,
                'hit_rate': hit_rate,
                'read_requests': requests,
                'disk_reads': reads
            }
            
        except Exception as e:
            logger.error(f"获取Buffer Pool状态失败: {e}")
            return None
    
    def check_performance(self):
        """检查性能并记录历史（仅监控，不做无效的SESSION级优化）"""
        status = self.get_buffer_pool_status()
        if not status:
            return False
        
        current_time = time.time()
        
        with self.lock:
            # 记录性能历史
            self.performance_history.append({
                'timestamp': current_time,
                'hit_rate': status['hit_rate'],
                'used_percent': status['used_percent'],
                'buffer_pool_size_mb': status['buffer_pool_size_mb']
            })
            
            # 只保留最近1小时的数据
            cutoff_time = current_time - 3600
            self.performance_history = [
                record for record in self.performance_history 
                if record['timestamp'] > cutoff_time
            ]
        
        # 性能评估（仅告警，Buffer Pool参数需在my.ini全局配置）
        hit_rate = status['hit_rate']
        if hit_rate < self.config['performance_threshold'] * 100:
            logger.warning(f"Buffer Pool命中率较低: {hit_rate:.2f}% (目标: {self.config['performance_threshold']*100:.0f}%)，建议在my.ini中调整innodb_buffer_pool_size")
        
        return True
    
    def get_performance_summary(self):
        """获取性能摘要"""
        with self.lock:
            if not self.performance_history:
                return {
                    'avg_hit_rate': 0.0,
                    'avg_used_percent': 0.0,
                    'records_count': 0,
                    'buffer_pool_size_mb': 0.0
                }
            
            recent_records = self.performance_history[-10:]  # 最近10条记录
            avg_hit_rate = sum(r['hit_rate'] for r in recent_records) / len(recent_records)
            avg_used_percent = sum(r['used_percent'] for r in recent_records) / len(recent_records)
            
            return {
                'avg_hit_rate': avg_hit_rate,
                'avg_used_percent': avg_used_percent,
                'records_count': len(self.performance_history),
                'buffer_pool_size_mb': recent_records[-1]['buffer_pool_size_mb'] if recent_records else 0
            }

# rt_k / rt_idx_k / rt_sw_k 是三个 Tushare 接口，官方各自 50 次/分钟，不是共用一桶。
_rtTmKw = dict(
    max_calls=TUSHARE_CONFIG['max_calls_per_minute'],
    baseInterval=TUSHARE_CONFIG['inter_call_interval'],
    maxInterval=TUSHARE_CONFIG['inter_call_interval_max'],
    intervalStep=TUSHARE_CONFIG['inter_call_interval_step'],
    recoverOk=TUSHARE_CONFIG['inter_call_recover_ok'],
)
time_manager = SimpleTimeManager(**_rtTmKw)
time_manager_idx = SimpleTimeManager(**_rtTmKw)
time_manager_sw = SimpleTimeManager(**_rtTmKw)

# 上次开始发请求的时刻（间隔从此时起算 1.2s，不是等返回后再等 1.2s）
_last_rt_api_call_start_time = None
_last_rt_idx_k_call_start_time = None
_last_rt_sw_k_call_start_time = None

def _sleep_until_next_call_allowed():
    """rt_k：距上次开始发请求满当前自适应间隔才发下一次。"""
    global _last_rt_api_call_start_time
    if _last_rt_api_call_start_time is None:
        return
    wait = _last_rt_api_call_start_time + time_manager.currentInterval() - time.time()
    if wait > 0:
        _sleep_interruptible(wait)

def _sleep_until_next_idx_call_allowed():
    """rt_idx_k：距上次开始发请求满当前自适应间隔才发下一次。"""
    global _last_rt_idx_k_call_start_time
    if _last_rt_idx_k_call_start_time is None:
        return
    wait = _last_rt_idx_k_call_start_time + time_manager_idx.currentInterval() - time.time()
    if wait > 0:
        _sleep_interruptible(wait)


def _sleep_until_next_sw_call_allowed():
    """rt_sw_k：距上次开始发请求满当前自适应间隔才发下一次。"""
    global _last_rt_sw_k_call_start_time
    if _last_rt_sw_k_call_start_time is None:
        return
    wait = _last_rt_sw_k_call_start_time + time_manager_sw.currentInterval() - time.time()
    if wait > 0:
        _sleep_interruptible(wait)


def _norm_trade_time(x):
    """统一 trade_time 为 'YYYY-MM-DD HH:MM:SS' 或 None，供 rt_k/rt_idx_k/rt_sw_k 写入 DATETIME 列；兼容 Timestamp/str/NaT。"""
    if x is None or pd.isna(x):
        return None
    if hasattr(x, 'strftime'):
        return x.strftime('%Y-%m-%d %H:%M:%S')
    s = str(x).strip()
    return s if s and s != 'NaT' else None


def isAShareTsCode(tsCode: str) -> bool:
    """沪深北 A 股（含 302）；排除 ETF/B 股/转债等。"""
    return bool(A_SHARE_TS_CODE_RE.match(str(tsCode or "").strip().upper()))


def _asQmtFloat(value):
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def firstBookLevel(tick, listKey: str, scalarKey: str):
    """五档 list 取第一档；否则用 *1 标量。"""
    if not isinstance(tick, dict):
        return None
    val = tick.get(listKey)
    if isinstance(val, (list, tuple)) and val:
        return val[0]
    return tick.get(scalarKey)


def parseQmtTradeTime(tick):
    """QMT time/stime → datetime。毫秒/秒时间戳；过小的数忽略。"""
    if not isinstance(tick, dict):
        return None
    raw = tick.get("time")
    if raw is None or raw == "":
        raw = tick.get("stime")
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > 1e12:
        return datetime.fromtimestamp(v / 1000.0)
    if v > 1e9:
        return datetime.fromtimestamp(v)
    return None


def qmtTickToRtKRow(tsCode: str, tick, asof=None):
    """瘦字段 → rt_k 行。vol/盘口量 手→股；无 name/num；非今日或 last<=0 返回 None。"""
    code = str(tsCode or "").strip().upper()
    if not isAShareTsCode(code) or not isinstance(tick, dict):
        return None
    last = _asQmtFloat(tick.get("lastPrice"))
    if last is None or last <= 0:
        return None
    tradeTime = parseQmtTradeTime(tick)
    if tradeTime is None:
        return None
    day = asof if asof is not None else datetime.now().date()
    if tradeTime.date() != day or tradeTime.hour < 9:
        return None
    vol = _asQmtFloat(tick.get("volume"))
    if vol is None:
        return None
    askP = _asQmtFloat(firstBookLevel(tick, "askPrice", "askPrice1"))
    bidP = _asQmtFloat(firstBookLevel(tick, "bidPrice", "bidPrice1"))
    askV = _asQmtFloat(firstBookLevel(tick, "askVol", "askVol1"))
    bidV = _asQmtFloat(firstBookLevel(tick, "bidVol", "bidVol1"))
    return {
        "ts_code": code,
        "pre_close": round(_asQmtFloat(tick.get("lastClose")) or 0.0, 4),
        "open": round(_asQmtFloat(tick.get("open")) or 0.0, 4),
        "high": round(_asQmtFloat(tick.get("high")) or 0.0, 4),
        "low": round(_asQmtFloat(tick.get("low")) or 0.0, 4),
        "close": round(last, 4),
        "vol": float(vol) * 100.0,
        "amount": _asQmtFloat(tick.get("amount")) or 0.0,
        "ask_price1": round(askP or 0.0, 4),
        "ask_volume1": float(askV or 0.0) * 100.0,
        "bid_price1": round(bidP or 0.0, 4),
        "bid_volume1": float(bidV or 0.0) * 100.0,
        "trade_time": tradeTime,
    }


def parseCacheTradeTime(value):
    """memory_cache / Tushare 的 trade_time → datetime，比新旧用。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime()
        except Exception:
            return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if hasattr(parsed, "to_pydatetime"):
        return parsed.to_pydatetime()
    return parsed


def isCacheNewerThanIncoming(cacheTime, incomingTime) -> bool:
    """缓存行情时刻更新则丢弃入站（禁止 Tushare 旧包回盖 QMT）。"""
    oldT = parseCacheTradeTime(cacheTime)
    newT = parseCacheTradeTime(incomingTime)
    if oldT is None or newT is None:
        return False
    return oldT > newT


def medianVolRatioOk(ratios) -> bool:
    if not ratios or len(ratios) < QMT_SANITY_N:
        return False
    med = statistics.median(ratios)
    return QMT_SANITY_RATIO_LO <= med <= QMT_SANITY_RATIO_HI


def decodeQmtPushPayload(blob):
    """与桥接 PUB 对称：msgpack 优先，失败回 JSON。"""
    if os.path.isdir(XTQUANT_SRC) and XTQUANT_SRC not in sys.path:
        sys.path.insert(0, XTQUANT_SRC)
    try:
        from bigqmt_signal_trader.quote_push_channel import decode_push_payload
        return decode_push_payload(blob)
    except Exception:
        pass
    if blob is None:
        return None
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    try:
        import msgpack
        return msgpack.unpackb(blob, raw=False)
    except Exception:
        pass
    return json.loads(blob.decode("utf-8"))


def qmtSilenceSec(nowMono: float | None = None) -> float:
    now = time.monotonic() if nowMono is None else nowMono
    with qmtLock:
        last = float(qmtState.get("lastPushMono") or 0.0)
    if last <= 0:
        return 1e9
    return now - last


def isQmtHealthy(nowMono: float | None = None) -> bool:
    """量纲抽样通过且全推未静默，才停 Tushare 三市场通配。"""
    now = time.monotonic() if nowMono is None else nowMono
    with qmtLock:
        if not qmtState.get("enabled"):
            qmtState["fallbackReason"] = "disabled"
            return False
        if qmtState.get("sanityOk") is not True:
            qmtState["fallbackReason"] = "sanity"
            return False
        last = float(qmtState.get("lastPushMono") or 0.0)
        started = float(qmtState.get("startedMono") or 0.0)
        uniqueN = len(qmtState.get("uniqueCodes") or ())
        allowN = len(qmtState.get("allowSet") or ())
    if last <= 0:
        with qmtLock:
            qmtState["fallbackReason"] = "no_push"
        return False
    if now - last > QMT_SILENCE_FALLBACK_SEC:
        with qmtLock:
            qmtState["fallbackReason"] = "silence"
        return False
    uniqueNeed = QMT_UNIQUE_MIN
    if allowN >= 100:
        uniqueNeed = min(QMT_UNIQUE_MIN, int(allowN * 0.85))
    if started > 0 and (now - started) >= QMT_WARMUP_SEC and uniqueN < uniqueNeed:
        with qmtLock:
            qmtState["fallbackReason"] = "unique"
        return False
    with qmtLock:
        qmtState["fallbackReason"] = ""
    return True


def shouldFetchTushareAShare() -> bool:
    return not isQmtHealthy()


def rememberAShareAllowCodes(codes) -> None:
    if codes is None:
        return
    added = {str(c).strip().upper() for c in codes if isAShareTsCode(c)}
    if not added:
        return
    with qmtLock:
        qmtState["allowSet"].update(added)


def setQmtPaused(paused: bool) -> None:
    with qmtLock:
        qmtState["paused"] = bool(paused)
        if paused:
            qmtState["dirty"].clear()


def recordQmtSanity(tsCode: str, qmtVol: float) -> None:
    """用 Tushare 垫层 vol 对 QMT×100 做一次中位比，防止手/股写反。"""
    code = str(tsCode).strip().upper()
    with cache_lock:
        if memory_cache.empty or code not in memory_cache.index or "vol" not in memory_cache.columns:
            return
        tv = memory_cache.at[code, "vol"]
    try:
        tushareVol = float(tv)
    except (TypeError, ValueError):
        return
    if tushareVol <= 0 or qmtVol <= 0:
        return
    ratio = qmtVol / tushareVol
    with qmtLock:
        if qmtState.get("sanityOk") is not None:
            return
        qmtState["sanityRatios"].append(ratio)
        ratios = list(qmtState["sanityRatios"])
    if len(ratios) < QMT_SANITY_N:
        return
    ok = medianVolRatioOk(ratios)
    med = statistics.median(ratios)
    with qmtLock:
        qmtState["sanityOk"] = ok
        if not ok:
            qmtState["enabled"] = False
    if ok:
        logger.info("QMT 量纲抽样通过 n=%d median=%.4f，个股改 QMT 主源", len(ratios), med)
    else:
        logger.error(
            "QMT 量纲抽样失败 n=%d median=%.4f，保持 Tushare 通配",
            len(ratios), med,
        )


def onQmtPush(_topic, data) -> None:
    if not isinstance(data, dict) or not data:
        return
    asof = datetime.now().date()
    now = time.monotonic()
    with qmtLock:
        qmtState["lastPushMono"] = now
        enabled = bool(qmtState.get("enabled"))
        paused = bool(qmtState.get("paused"))
        sanityOk = qmtState.get("sanityOk")
        allowSet = qmtState.get("allowSet") or set()
    if not enabled:
        return
    accepted = 0
    for rawCode, tick in data.items():
        code = str(rawCode or "").strip().upper()
        if not isAShareTsCode(code):
            continue
        with qmtLock:
            qmtState["uniqueCodes"].add(code)
        row = qmtTickToRtKRow(code, tick, asof)
        if row is None:
            continue
        if code not in allowSet:
            continue
        if sanityOk is not True:
            recordQmtSanity(code, row["vol"])
            continue
        if paused:
            continue
        with qmtLock:
            qmtState["dirty"][code] = row
        accepted += 1
    if accepted:
        update_stats("qmt_ticks", accepted)


def qmtSubLoop() -> None:
    """空前缀 SUB 15574；禁止再订全推。socket 只在本线程关闭。"""
    try:
        import zmq
    except Exception as exc:
        logger.error("QMT SUB 未启动：无 pyzmq %s", exc)
        return
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.RCVHWM, 512)
    sub.setsockopt(zmq.LINGER, 0)
    sub.connect(QMT_PUSH_ADDR)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    poller = zmq.Poller()
    poller.register(sub, zmq.POLLIN)
    logger.info("QMT PUB SUB 已连接 %s（空前缀，不订 combo）", QMT_PUSH_ADDR)
    try:
        while running and not _qmtStop.is_set():
            try:
                events = dict(poller.poll(200))
            except Exception:
                break
            if sub not in events:
                continue
            try:
                frames = sub.recv_multipart(zmq.NOBLOCK)
            except Exception:
                continue
            if len(frames) < 2:
                continue
            try:
                payload = decodeQmtPushPayload(frames[-1])
            except Exception as exc:
                logger.debug("QMT 解码失败: %s", exc)
                continue
            data = payload.get("data") if isinstance(payload, dict) else payload
            try:
                onQmtPush(frames[0], data)
            except Exception as exc:
                logger.warning("QMT 回调失败: %s", exc)
    finally:
        try:
            sub.close(linger=0)
        except Exception:
            pass


def flushQmtDirty() -> None:
    with qmtLock:
        if qmtState.get("paused") or qmtState.get("sanityOk") is not True:
            return
        dirty = qmtState["dirty"]
        qmtState["dirty"] = {}
    if not dirty:
        return
    df = pd.DataFrame(list(dirty.values()))
    if df.empty:
        return
    processed = validate_and_clean_data(df)
    if processed is None or processed.empty:
        return
    if "ts_code" in processed.columns:
        processed = processed.set_index("ts_code")
    with cache_lock:
        global memory_cache
        memory_cache = ensure_rt_k_float_dtypes(memory_cache)
        overlap = processed.index.intersection(memory_cache.index)
        if len(overlap) == 0:
            return
        memory_cache.update(processed.loc[overlap])
        writeDf = processed.loc[overlap]
    if writeDf.empty:
        return
    sync_db_write_batch(writeDf, preserveNameNum=True)
    update_stats("qmt_writes", len(writeDf))


def qmtFlushLoop() -> None:
    while running and not _qmtStop.is_set():
        _sleep_interruptible(QMT_FLUSH_SEC)
        if not running or _qmtStop.is_set():
            break
        try:
            flushQmtDirty()
        except Exception as exc:
            logger.error("QMT 刷库失败: %s", exc)
            update_stats("errors")


def startQmtThreads() -> None:
    if not RT_K_QMT_PRIMARY:
        logger.info("RT_K_QMT_PRIMARY=0，个股仍走 Tushare 通配")
        return
    with qmtLock:
        qmtState["startedMono"] = time.monotonic()
        qmtState["enabled"] = True
    threading.Thread(target=qmtSubLoop, name="tusharert-qmt-sub", daemon=True).start()
    threading.Thread(target=qmtFlushLoop, name="tusharert-qmt-flush", daemon=True).start()


def defaultTusharertLockPath() -> str:
    """固定目录：不跟 cwd / 脚本副本走，PyCharm 与 Cursor 共用一把锁。"""
    override = (os.environ.get("TUSHARERT_INSTANCE_LOCK_PATH") or "").strip()
    if override:
        parent = os.path.dirname(os.path.abspath(override))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return os.path.abspath(override)
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TEMP")
        or os.path.dirname(os.path.abspath(__file__))
    )
    lockDir = os.path.join(base, "tusharert")
    os.makedirs(lockDir, exist_ok=True)
    return os.path.join(lockDir, INSTANCE_LOCK_NAME)


def tusharertLegacyLockPath() -> str | None:
    """旧版锁在 %TEMP%；能占则占，避免只认 TEMP 的旧脚本再启动。"""
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not tmp:
        return None
    legacy = os.path.abspath(os.path.join(tmp, INSTANCE_LOCK_NAME))
    primary = os.path.abspath(defaultTusharertLockPath())
    if os.path.normcase(legacy) == os.path.normcase(primary):
        return None
    return legacy


def isTusharertScriptCmd(argv) -> bool:
    """命令行是否在跑 tusharert 主脚本（排除 unittest / python -c）。"""
    for part in argv or []:
        base = os.path.basename(str(part)).lower()
        if base.startswith("tusharert") and base.endswith(".py"):
            return True
    return False


def listOtherTusharertPids() -> list:
    """其它仍在跑的 tusharert python 进程。"""
    me = os.getpid()
    found = []
    try:
        import psutil
    except ImportError:
        return found
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid == me:
                continue
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            if isTusharertScriptCmd(proc.info.get("cmdline") or []):
                found.append(pid)
        except (psutil.Error, TypeError, ValueError):
            continue
    return found


def _readLockHolderPid(lockPath: str) -> str:
    try:
        with open(lockPath, "r", encoding="ascii", errors="ignore") as f:
            text = (f.read() or "").strip()
        if not text:
            return "?"
        return text.split()[0][:16]
    except OSError:
        return "?"


def tryAcquireTusharertLock(lockPath: str):
    lockFile = open(lockPath, "a+b")
    try:
        lockFile.seek(0, os.SEEK_END)
        if lockFile.tell() == 0:
            lockFile.write(b"\0")
            lockFile.flush()
        lockFile.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lockFile.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lockFile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lockFile.close()
        return None
    try:
        lockFile.seek(0)
        lockFile.truncate(0)
        lockFile.write(("%d\n" % os.getpid()).encode("ascii"))
        lockFile.flush()
    except OSError:
        pass
    return lockFile


def tryAcquireTusharertMutex():
    """Windows 命名互斥体：与工作目录、TEMP、脚本副本路径均无关。"""
    if os.name != "nt":
        return True, None
    import ctypes
    kernel32 = ctypes.windll.kernel32
    errorAlreadyExists = 183
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        logger.error("CreateMutexW 失败，无法建立单实例锁")
        return False, None
    if kernel32.GetLastError() == errorAlreadyExists:
        kernel32.CloseHandle(handle)
        return False, None
    return True, handle


def _releaseTusharertMutex() -> None:
    global _instanceMutexHandle
    handle = _instanceMutexHandle
    _instanceMutexHandle = None
    if handle is None:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def acquireTusharertSingleton() -> bool:
    """本机只允许一个 tusharert 写 rt_k（PyCharm / Cursor / 任意副本）。"""
    global _instanceLockFile, _instanceLockFiles, _instanceMutexHandle
    others = listOtherTusharertPids()
    if others:
        logger.error(
            "另一 tusharert 进程仍在运行 PID=%s。请先结束该进程再启动，避免双写 rt_k。",
            ",".join(str(pid) for pid in others),
        )
        return False

    ok, mutexHandle = tryAcquireTusharertMutex()
    if not ok:
        logger.error(
            "另一 tusharert 实例已在运行（互斥体 %s）。同时只允许一份写 rt_k。",
            INSTANCE_MUTEX_NAME,
        )
        return False
    _instanceMutexHandle = mutexHandle

    primary = defaultTusharertLockPath()
    lockFile = tryAcquireTusharertLock(primary)
    if lockFile is None:
        holder = _readLockHolderPid(primary)
        logger.error(
            "另一 tusharert 实例已在写 rt_k，锁文件 %s（占用 PID %s）",
            primary,
            holder,
        )
        _releaseTusharertMutex()
        return False
    held = [lockFile]
    logger.info("实例锁已占用 %s pid=%s", primary, os.getpid())

    legacy = tusharertLegacyLockPath()
    if legacy:
        legacyFile = tryAcquireTusharertLock(legacy)
        if legacyFile is None:
            logger.warning(
                "旧版 TEMP 锁未能占用 %s；互斥体与主锁已持有，忽略后继续启动",
                legacy,
            )
        else:
            held.append(legacyFile)
            logger.info("实例锁已占用 %s pid=%s", legacy, os.getpid())

    _instanceLockFiles = held
    _instanceLockFile = held[0]
    atexit.register(releaseTusharertSingleton)
    return True


def releaseTusharertLock(lockFile) -> None:
    if lockFile is None:
        return
    try:
        lockFile.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lockFile.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lockFile.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        lockFile.close()
    except OSError:
        pass


def releaseTusharertSingleton() -> None:
    global _instanceLockFile, _instanceLockFiles
    for lockFile in list(_instanceLockFiles):
        releaseTusharertLock(lockFile)
    _instanceLockFiles = []
    _instanceLockFile = None
    _releaseTusharertMutex()


def filterTushareRowsAgainstCache(processedDf: pd.DataFrame) -> pd.DataFrame:
    """A 股若缓存 trade_time 更新则丢弃该行；基金主题不受影响。"""
    if processedDf is None or processedDf.empty:
        return processedDf
    with cache_lock:
        if memory_cache.empty or "trade_time" not in memory_cache.columns:
            return processedDf
        dropCodes = []
        for code in processedDf.index:
            if not isAShareTsCode(str(code)):
                continue
            if code not in memory_cache.index:
                continue
            if isCacheNewerThanIncoming(
                memory_cache.at[code, "trade_time"],
                processedDf.at[code, "trade_time"] if "trade_time" in processedDf.columns else None,
            ):
                dropCodes.append(code)
    if not dropCodes:
        return processedDf
    return processedDf.drop(index=dropCodes)

# 创建连接池和API
try:
    connection_pool = mysql.connector.pooling.MySQLConnectionPool(**DB_CONFIG)
    logger.info("数据库连接池(stock_wind)创建成功")
except Exception as e:
    logger.error(f"数据库连接池(stock_wind)创建失败: {e}")
    connection_pool = None

try:
    connection_pool_tushare = mysql.connector.pooling.MySQLConnectionPool(**DB_CONFIG_TUSHARE)
    logger.info("数据库连接池(stock_tushare)创建成功")
except Exception as e:
    logger.error(f"数据库连接池(stock_tushare)创建失败: {e}")
    connection_pool_tushare = None

try:
    pro = ts.pro_api(TUSHARE_CONFIG['token'])
    logger.info("Tushare Pro接口初始化成功")
except Exception as e:
    logger.error(f"Tushare Pro接口初始化失败: {e}")
    pro = None

# 创建Buffer Pool监控器
buffer_pool_monitor = BufferPoolMonitor(connection_pool, BUFFER_POOL_CONFIG) if connection_pool else None

def rt_k_table_is_empty() -> bool:
    """检查 rt_k 表是否无数据（0 条）。用于表空时先拉一次、不先清空。"""
    if not connection_pool:
        return True
    conn = None
    cursor = None
    try:
        conn = connection_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM rt_k")
        (cnt,) = cursor.fetchone()
        return cnt == 0
    except Exception:
        return True
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def clear_rt_k_table():
    """清空 rt_k 表，保证本地与数据源一致（清除退市股/更名股等残留）。"""
    if not connection_pool:
        logger.error("数据库连接池未初始化，无法清空表")
        return False
        
    connection = None
    cursor = None
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor()
        cursor.execute("TRUNCATE TABLE rt_k")
        connection.commit()
        logger.info("✅ rt_k表已清空，等待从数据源重新获取最新数据")
        return True
    except mysql.connector.Error as err:
        logger.error(f"清空rt_k表失败: {err}")
        if connection:
            connection.rollback()
        return False
    except Exception as e:
        logger.error(f"清空rt_k表时发生未知错误: {e}")
        if connection:
            connection.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 批量写入数据库 - 向量化优化版
def sync_db_write_batch(data_df, preserveNameNum: bool = False):
    """批量写入数据库。preserveNameNum=True 时不覆盖 name/num（QMT 无这两列）。"""
    if not connection_pool:
        logger.error("数据库连接池未初始化，无法写入数据")
        return
    if data_df.empty:
        logger.info("sync_db_write_batch 跳过：数据为空，未写入 rt_k")
        return
    logger.info("sync_db_write_batch 开始写入 rt_k，共 %d 条", len(data_df))
        
    connection = None
    cursor = None
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor()
        
        # 仅设置有效的会话级参数（innodb_flush_log_at_trx_commit 支持 SESSION 级修改）
        try:
            cursor.execute("SET SESSION innodb_flush_log_at_trx_commit = 2")
        except Exception as e:
            logger.debug(f"会话级优化参数设置失败: {e}")
        
        # SQL预处理语句
        if preserveNameNum:
            sql = """
            INSERT INTO rt_k 
            (ts_code, name, pre_close, high, open, low, close, vol, amount, num, ask_price1, ask_volume1, bid_price1, bid_volume1, trade_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            pre_close = VALUES(pre_close),
            high = VALUES(high),
            open = VALUES(open),
            low = VALUES(low),
            close = VALUES(close),
            vol = VALUES(vol),
            amount = VALUES(amount),
            ask_price1 = VALUES(ask_price1),
            ask_volume1 = VALUES(ask_volume1),
            bid_price1 = VALUES(bid_price1),
            bid_volume1 = VALUES(bid_volume1),
            trade_time = VALUES(trade_time)
            """
        else:
            sql = """
            INSERT INTO rt_k 
            (ts_code, name, pre_close, high, open, low, close, vol, amount, num, ask_price1, ask_volume1, bid_price1, bid_volume1, trade_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            name = VALUES(name),
            pre_close = VALUES(pre_close),
            high = VALUES(high),
            open = VALUES(open),
            low = VALUES(low),
            close = VALUES(close),
            vol = VALUES(vol),
            amount = VALUES(amount),
            num = VALUES(num),
            ask_price1 = VALUES(ask_price1),
            ask_volume1 = VALUES(ask_volume1),
            bid_price1 = VALUES(bid_price1),
            bid_volume1 = VALUES(bid_volume1),
            trade_time = VALUES(trade_time)
            """
        
        # --- 向量化构建批量数据（不再逐行逐字段验证，数据已在上游清洗完毕） ---
        db_fields = ['name', 'pre_close', 'high', 'open', 'low', 'close', 'vol', 'amount',
                     'num', 'ask_price1', 'ask_volume1', 'bid_price1', 'bid_volume1', 'trade_time']
        
        write_df = data_df.copy()
        
        # trade_time：与 rt_idx_k 一致归一化，兼容 API 返回 Timestamp/str 等，避免 AttributeError
        if 'trade_time' in write_df.columns:
            write_df['trade_time'] = write_df['trade_time'].apply(_norm_trade_time)
        
        # 将 ts_code（index）恢复为列，按字段顺序排列
        export_df = write_df.reset_index()
        export_cols = ['ts_code'] + db_fields
        for col in export_cols:
            if col not in export_df.columns:
                export_df[col] = None
        export_df = export_df[export_cols]
        
        # NaN → None（mysql.connector 将 None 作为 NULL 处理）
        export_df = export_df.astype(object).where(export_df.notna(), None)
        
        # 高效构建元组列表（itertuples 比 iterrows 快 10-50 倍）
        batch_data_all = [tuple(row) for row in export_df.itertuples(index=False, name=None)]
        
        # 分批写入（数据已预处理，可使用更大批次）
        batch_size = 500
        total_processed = 0
        
        for i in range(0, len(batch_data_all), batch_size):
            batch = batch_data_all[i:i + batch_size]
            
            # 死锁重试机制
            max_retries = 3
            for retry_count in range(max_retries):
                try:
                    cursor.executemany(sql, batch)
                    total_processed += len(batch)
                    connection.commit()
                    break
                except mysql.connector.Error as err:
                    if err.errno == 1213:  # 死锁
                        connection.rollback()
                        if retry_count < max_retries - 1:
                            logger.warning(f"检测到死锁，第{retry_count + 1}次重试...")
                            time.sleep(0.1 * (retry_count + 1))
                        else:
                            logger.error(f"死锁重试{max_retries}次后仍然失败，跳过此批次")
                    else:
                        raise err
        
        update_stats('db_writes', total_processed)
        logger.info("rt_k 写入成功，共 %d 条", total_processed)
        
    except mysql.connector.Error as err:
        logger.error(f"批量写入失败: {err}")
        update_stats('errors')
        if connection:
            connection.rollback()
    except Exception as e:
        logger.error(f"批量写入时发生未知错误: {e}")
        update_stats('errors')
        if connection:
            connection.rollback()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# ---------- rt_idx_k 指数实时日线（stock_tushare，独立限流 50 次/分钟）----------
def create_rt_idx_k_table_if_not_exists():
    """建表：stock_tushare.rt_idx_k，主键 ts_code"""
    if not connection_pool_tushare:
        return False
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{RT_IDX_K_TABLE}` (
      `ts_code`     VARCHAR(20)   NOT NULL COMMENT '指数代码',
      `name`        VARCHAR(80)   DEFAULT NULL COMMENT '指数名称',
      `trade_time`  DATETIME      DEFAULT NULL COMMENT '交易时间（与 rt_k 一致）',
      `close`       DECIMAL(16,4) DEFAULT NULL COMMENT '现价',
      `pre_close`   DECIMAL(16,4) DEFAULT NULL COMMENT '昨收',
      `high`        DECIMAL(16,4) DEFAULT NULL COMMENT '最高价',
      `open`        DECIMAL(16,4) DEFAULT NULL COMMENT '开盘价',
      `low`         DECIMAL(16,4) DEFAULT NULL COMMENT '最低价',
      `vol`         DOUBLE        DEFAULT NULL COMMENT '成交量',
      `amount`      DOUBLE        DEFAULT NULL COMMENT '成交金额(元)',
      PRIMARY KEY (`ts_code`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易所指数实时日线(rt_idx_k轮询)';
    """
    conn = None
    try:
        conn = connection_pool_tushare.get_connection()
        cursor = conn.cursor()
        cursor.execute(ddl)
        conn.commit()
        logger.info("表 %s 已就绪", RT_IDX_K_TABLE)
        return True
    except mysql.connector.Error as err:
        logger.error("建表 %s 失败: %s", RT_IDX_K_TABLE, err)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def rt_idx_k_table_is_empty() -> bool:
    """检查 rt_idx_k 表是否无数据（与 rt_k 同一业务逻辑：表空先拉一次）。"""
    if not connection_pool_tushare:
        return True
    conn = None
    cursor = None
    try:
        conn = connection_pool_tushare.get_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM `{RT_IDX_K_TABLE}`")
        (cnt,) = cursor.fetchone()
        return cnt == 0
    except Exception:
        return True
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def clear_rt_idx_k_table() -> bool:
    """清空 rt_idx_k 中 Tushare 可回补的行；保留 TQ 写入的 932000.CSI / 899050.BJ。"""
    if not connection_pool_tushare:
        return False
    conn = None
    cursor = None
    try:
        conn = connection_pool_tushare.get_connection()
        cursor = conn.cursor()
        # 勿 TRUNCATE：会抹掉通达信 tdx_opt_live 补的中证2000/北证50
        cursor.execute(
            f"DELETE FROM `{RT_IDX_K_TABLE}` WHERE ts_code NOT IN (%s, %s)",
            ("932000.CSI", "899050.BJ"),
        )
        conn.commit()
        logger.info(
            "✅ %s 已按条件清空（保留 TQ 指数 932000.CSI/899050.BJ），等待从数据源重新获取",
            RT_IDX_K_TABLE,
        )
        return True
    except mysql.connector.Error as err:
        logger.error("清空 %s 失败: %s", RT_IDX_K_TABLE, err)
        if conn:
            conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def save_rt_idx_k_batch(data_df):
    """将指数 DataFrame 写入 stock_tushare.rt_idx_k，REPLACE INTO；trade_time 与 rt_k 一致归一化为 DATETIME 可写格式"""
    if not connection_pool_tushare or data_df is None or data_df.empty:
        return 0
    cols = ['ts_code', 'name', 'trade_time', 'close', 'pre_close', 'high', 'open', 'low', 'vol', 'amount']
    df = data_df.reindex(columns=cols).copy()
    if 'trade_time' in df.columns:
        df['trade_time'] = df['trade_time'].apply(_norm_trade_time)
    df = df.where(pd.notna(df), None)
    rows = [tuple(x) for x in df.to_numpy().tolist()]
    sql = f"""
    REPLACE INTO `{RT_IDX_K_TABLE}` (ts_code, name, trade_time, close, pre_close, high, open, low, vol, amount)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    conn = None
    try:
        conn = connection_pool_tushare.get_connection()
        cursor = conn.cursor()
        cursor.executemany(sql, rows)
        conn.commit()
        n = len(rows)
        update_stats('idx_db_writes', n)
        logger.info("rt_idx_k 写入 %d 条到 %s", n, RT_IDX_K_TABLE)
        return n
    except mysql.connector.Error as err:
        logger.error("写入 %s 失败: %s", RT_IDX_K_TABLE, err)
        update_stats('errors')
        if conn:
            conn.rollback()
        return 0
    finally:
        if conn:
            conn.close()

def fetch_realtime_idx_data():
    """获取交易所指数实时日线 rt_idx_k，独立限流（50 次/分钟），间隔从开始发请求起算。"""
    global _last_rt_idx_k_call_start_time
    if not pro or not ENABLE_RT_IDX_K:
        return None
    all_data = []
    for pattern in IDX_PATTERNS:
        if not running:
            break
        if not _waitUntilApiSlot(time_manager_idx, "rt_idx_k"):
            break
        try:
            logger.info("正在获取指数实时日线 %s ...", pattern)
            _sleep_until_next_idx_call_allowed()
            time_manager_idx.record_call()
            update_stats('tushare_idx_api_calls')
            _last_rt_idx_k_call_start_time = time.time()
            df = pro.rt_idx_k(ts_code=pattern, fields=IDX_FIELDS)
            time_manager_idx.noteCallOk()
            if df is not None and not df.empty:
                all_data.append(df)
                logger.info("rt_idx_k %s 获取 %d 条", pattern, len(df))
        except Exception as e:
            logger.warning("rt_idx_k %s 失败: %s", pattern, e)
            if isTushareFreqLimited(e):
                _noteFreqLimited(time_manager_idx, "rt_idx_k", pattern, e)
                break
            if "权限" in str(e) or "积分" in str(e):
                logger.warning("可能未开通指数实时权限，请检查")
    if not all_data:
        return None
    combined = pd.concat(all_data, ignore_index=True)
    combined = combined.drop_duplicates(subset=['ts_code'], keep='first')
    return combined


# ---------- rt_sw_k 申万行业实时截面（stock_tushare，独立限流，与 rt_idx_k 分桶）----------
def create_rt_sw_k_table_if_not_exists():
    """建表：stock_tushare.rt_sw_k（与独立脚本 rt_sw_k.py DDL 对齐）。"""
    if not connection_pool_tushare:
        return False
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{RT_SW_K_TABLE}` (
      `ts_code` varchar(20) NOT NULL COMMENT '指数代码',
      `name` varchar(80) DEFAULT NULL COMMENT '指数名称',
      `trade_time` datetime DEFAULT NULL COMMENT '交易时间',
      `close` decimal(10,4) DEFAULT NULL COMMENT '现价',
      `pre_close` decimal(10,4) DEFAULT NULL COMMENT '昨收',
      `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
      `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
      `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
      `vol` decimal(24,0) DEFAULT NULL COMMENT '成交量（股）',
      `amount` decimal(24,2) DEFAULT NULL COMMENT '成交金额（元）',
      `pct_change` decimal(12,4) DEFAULT NULL COMMENT '涨跌幅',
      `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
      PRIMARY KEY (`ts_code`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='申万行业指数实时截面(rt_sw_k)';
    """
    conn = None
    try:
        conn = connection_pool_tushare.get_connection()
        cursor = conn.cursor()
        cursor.execute(ddl)
        conn.commit()
        logger.info("表 %s 已就绪", RT_SW_K_TABLE)
        return True
    except mysql.connector.Error as err:
        logger.error("建表 %s 失败: %s", RT_SW_K_TABLE, err)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def clear_rt_sw_k_table() -> bool:
    """进入轮询窗口时清空 rt_sw_k，与 rt_idx_k 语义一致。"""
    if not connection_pool_tushare:
        return False
    conn = None
    cursor = None
    try:
        conn = connection_pool_tushare.get_connection()
        cursor = conn.cursor()
        cursor.execute(f"TRUNCATE TABLE `{RT_SW_K_TABLE}`")
        conn.commit()
        logger.info("✅ %s 表已清空，等待从数据源重新获取", RT_SW_K_TABLE)
        return True
    except mysql.connector.Error as err:
        logger.error("清空 %s 失败: %s", RT_SW_K_TABLE, err)
        if conn:
            conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _prepare_rt_sw_k_dataframe(data_df):
    """数值与时间与独立 rt_sw_k.py 对齐，便于写入 DECIMAL 列。"""
    if data_df is None or data_df.empty:
        return data_df
    out = data_df.copy()
    price_cols = ['close', 'pre_close', 'high', 'open', 'low']
    for c in price_cols:
        if c not in out.columns:
            continue
        out[c] = pd.to_numeric(out[c], errors='coerce').map(
            lambda x: (
                None
                if pd.isna(x)
                else float(
                    Decimal(str(x)).quantize(
                        Decimal('0.0001'), rounding=ROUND_HALF_UP
                    )
                )
            )
        )
    if 'trade_time' in out.columns:
        out['trade_time'] = out['trade_time'].map(_norm_trade_time)
    if 'vol' in out.columns:
        out['vol'] = pd.to_numeric(out['vol'], errors='coerce').map(
            lambda x: (
                None
                if pd.isna(x)
                else int(Decimal(str(x)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            )
        )
    if 'amount' in out.columns:
        out['amount'] = pd.to_numeric(out['amount'], errors='coerce').map(
            lambda x: (
                None
                if pd.isna(x)
                else float(
                    Decimal(str(x)).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                )
            )
        )
    if 'pct_change' in out.columns:
        out['pct_change'] = pd.to_numeric(out['pct_change'], errors='coerce').map(
            lambda x: (
                None
                if pd.isna(x)
                else float(
                    Decimal(str(x)).quantize(
                        Decimal('0.0001'), rounding=ROUND_HALF_UP
                    )
                )
            )
        )
    if 'name' in out.columns:
        out['name'] = out['name'].map(
            lambda x: None if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)[:80]
        )
    if 'ts_code' in out.columns:
        out['ts_code'] = out['ts_code'].astype(str).str.strip()
        out = out[out['ts_code'] != '']
    out = out.drop_duplicates(subset=['ts_code'], keep='last')
    out = out.where(pd.notna(out), None)
    return out


def save_rt_sw_k_batch(data_df):
    """写入 stock_tushare.rt_sw_k，REPLACE INTO（含 pct_change）。"""
    if not connection_pool_tushare or data_df is None or data_df.empty:
        return 0
    cols = [
        'ts_code', 'name', 'trade_time', 'close', 'pre_close', 'high', 'open', 'low',
        'vol', 'amount', 'pct_change',
    ]
    df = _prepare_rt_sw_k_dataframe(data_df)
    if df is None or df.empty:
        return 0
    if 'close' in df.columns:
        closeNum = pd.to_numeric(df['close'], errors='coerce')
        keep = closeNum.notna() & (closeNum > 0)
        dropped = int((~keep).sum())
        if dropped:
            logger.warning('rt_sw_k 丢弃不可用行 %s（close 缺失或<=0）', dropped)
        df = df.loc[keep].copy()
        if df.empty:
            logger.warning('rt_sw_k 本批全部 close<=0，跳过写入以免覆盖')
            return 0
    df = df.reindex(columns=cols).copy()
    df = df.where(pd.notna(df), None)
    rows = [tuple(x) for x in df.to_numpy().tolist()]
    sql = f"""
    REPLACE INTO `{RT_SW_K_TABLE}`
    (ts_code, name, trade_time, close, pre_close, high, open, low, vol, amount, pct_change)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    conn = None
    try:
        conn = connection_pool_tushare.get_connection()
        cursor = conn.cursor()
        cursor.executemany(sql, rows)
        conn.commit()
        n = len(rows)
        update_stats('sw_db_writes', n)
        logger.info("rt_sw_k 写入 %d 条到 %s", n, RT_SW_K_TABLE)
        return n
    except mysql.connector.Error as err:
        logger.error("写入 %s 失败: %s", RT_SW_K_TABLE, err)
        update_stats('errors')
        if conn:
            conn.rollback()
        return 0
    finally:
        if conn:
            conn.close()


def fetch_realtime_sw_data():
    """申万 rt_sw_k 全量截面，一次 API；独立限流（50 次/分钟桶）。"""
    global _last_rt_sw_k_call_start_time
    if not pro or not ENABLE_RT_SW_K:
        return None
    if not _waitUntilApiSlot(time_manager_sw, "rt_sw_k"):
        return None
    try:
        logger.info("正在获取申万实时截面 rt_sw_k ...")
        _sleep_until_next_sw_call_allowed()
        time_manager_sw.record_call()
        update_stats('tushare_sw_api_calls')
        _last_rt_sw_k_call_start_time = time.time()
        df = pro.rt_sw_k(fields=RT_SW_K_FIELDS)
        time_manager_sw.noteCallOk()
        if df is not None and not df.empty:
            logger.info("rt_sw_k 获取 %d 条", len(df))
        return df
    except Exception as e:
        logger.warning("rt_sw_k 失败: %s", e)
        if isTushareFreqLimited(e):
            _noteFreqLimited(time_manager_sw, "rt_sw_k", "full", e)
        elif "权限" in str(e) or "积分" in str(e):
            logger.warning("可能未开通申万实时 rt_sw_k 权限，请检查")
        return None


# 获取实时数据
def fetch_realtime_stock_data(includeStockPatterns: bool = True):
    """获取实时股票数据。QMT 健康时跳过三市场通配，基金主题始终拉。"""
    global _last_rt_api_call_start_time
    try:
        all_data = []
        rateLimited = False

        # 1. 处理普通市场数据
        patterns = STOCK_PATTERNS if includeStockPatterns else []
        if not includeStockPatterns:
            logger.info("QMT 个股主源健康，本轮跳过 Tushare *.SH/*.SZ/*.BJ 通配")
        for pattern in patterns:
            if rateLimited or not running:
                break
            if not _waitUntilApiSlot(time_manager, "rt_k"):
                break

            try:
                logger.info(f"正在获取 {pattern} 模式的股票数据...")
                _sleep_until_next_call_allowed()
                time_manager.record_call()
                update_stats('tushare_api_calls')
                _last_rt_api_call_start_time = time.time()
                df = pro.rt_k(ts_code=pattern, fields=','.join(FIELDS_LIST))
                time_manager.noteCallOk()

                if df is not None and not df.empty:
                    logger.info(f"成功获取 {pattern} 模式数据，共 {len(df)} 条记录")
                    all_data.append(df)
                else:
                    logger.warning(f"{pattern} 模式获取的数据为空")

            except Exception as e:
                logger.error(f"获取 {pattern} 模式数据失败: {e}")
                update_stats('errors')
                if isTushareFreqLimited(e):
                    _noteFreqLimited(time_manager, "rt_k", pattern, e)
                    rateLimited = True
                    break
                continue

        # 2. 处理特殊主题数据（通配已限频则本轮不再打基金主题）
        for topic_config in SPECIAL_TOPICS:
            if rateLimited or not running:
                break
            if not _waitUntilApiSlot(time_manager, "rt_k"):
                break

            try:
                logger.info(f"正在获取特殊主题数据: {topic_config['name']}")
                _sleep_until_next_call_allowed()
                time_manager.record_call()
                update_stats('tushare_api_calls')
                _last_rt_api_call_start_time = time.time()
                df = pro.rt_k(
                    ts_code=topic_config['ts_code'],
                    topic=topic_config['topic'],
                    fields=','.join(FIELDS_LIST),
                )
                time_manager.noteCallOk()

                if df is not None and not df.empty:
                    logger.info(f"成功获取特殊主题数据，共 {len(df)} 条记录")
                    all_data.append(df)
                else:
                    logger.warning(f"特殊主题数据为空")

            except Exception as e:
                logger.error(f"获取特殊主题数据失败: {e}")
                update_stats('errors')
                if isTushareFreqLimited(e):
                    _noteFreqLimited(time_manager, "rt_k", topic_config['name'], e)
                    rateLimited = True
                    break
                continue
        
        if all_data:
            combined_df = pd.concat(all_data, ignore_index=True)
            # 先股票通配、后基金主题：同码保留后者，避免 *.SZ 股票主题里的冻盘 ETF 盖住 HQ_FND_TICK
            combined_df = combined_df.drop_duplicates(subset=['ts_code'], keep='last')
            
            total_records = len(combined_df)
            logger.info(f"轮询完成，总共获取 {total_records} 条股票数据（包含普通市场和特殊主题）")
            
            return combined_df
        else:
            logger.info("本次轮询未获取到新数据")
            return None
            
    except Exception as e:
        logger.error(f"获取实时数据失败: {e}")
        update_stats('errors')
        return None

# 处理实时数据
def process_realtime_data(data_df, skipCacheNewerFilter: bool = False):
    """处理实时数据。skipCacheNewerFilter 用于清表后强制回填，避免缓存较新把 A 股整批挡掉。"""
    if data_df is None or data_df.empty:
        logger.info("process_realtime_data 跳过：输入无数据（0 条），不写库")
        return
    in_rows = len(data_df)
    try:
        # 使用新的数据验证函数
        processed_df = validate_and_clean_data(data_df)
        
        if 'ts_code' in processed_df.columns:
            processed_df.set_index('ts_code', inplace=True)
        rememberAShareAllowCodes(processed_df.index)
        if not skipCacheNewerFilter:
            processed_df = filterTushareRowsAgainstCache(processed_df)
            if processed_df.empty:
                logger.info("Tushare 本批 A 股均被更新的 QMT 缓存挡住，只保留主题/新码")
                return
        
        with cache_lock:
            global memory_cache
            
            # 向量化更新：已有行原地覆盖，新增行追加（替代逐行 iterrows）
            if memory_cache.empty:
                # 首次运行缓存为空，直接整体赋值，避免空DataFrame参与concat产生FutureWarning
                memory_cache = processed_df.copy()
            else:
                # 历史缓存可能仍为 int64（旧轮次 validate 未统一 dtype），更新前对齐为 float64
                memory_cache = ensure_rt_k_float_dtypes(memory_cache)
                memory_cache.update(processed_df)
                new_codes = processed_df.index.difference(memory_cache.index)
                if len(new_codes) > 0:
                    memory_cache = pd.concat([memory_cache, processed_df.loc[new_codes]])
        
        update_stats('data_updates', len(processed_df))
        update_stats('last_update_time', time.time())
        
        logger.info("📊 数据更新完成，验证后 %d 条，准备写库", len(processed_df))
        if processed_df.empty:
            logger.warning("validate_and_clean_data 后无有效行，跳过写库")
            return
        sync_db_write_batch(processed_df)
        
    except Exception as e:
        logger.error(f"处理实时数据时发生错误: {e}")
        update_stats('errors')

# 显示运行状态
def show_running_status():
    """显示运行状态"""
    if not stats['start_time']:
        return
        
    current_time = time.time()
    uptime_minutes = int((current_time - stats['start_time']) / 60)
    uptime_seconds = int(current_time - stats['start_time']) % 60
    
    update_frequency = "N/A"
    if stats['data_updates'] > 0 and stats['last_update_time']:
        time_since_update = current_time - stats['last_update_time']
        if time_since_update > 0:
            update_frequency = f"{stats['data_updates'] / (time_since_update / 60):.2f} 次/分钟"
    
    logger.info("=" * 60)
    logger.info("📊 Tushare实时个股行情程序运行状态报告")
    logger.info("=" * 60)
    logger.info(f"⏱️  运行时间: {uptime_minutes}分{uptime_seconds}秒")
    logger.info(f"📈 数据更新次数: {stats['data_updates']}")
    logger.info(f"💾 数据库写入次数: {stats['db_writes']}")
    logger.info(f"🔄 更新频率: {update_frequency}")
    logger.info(f"🌐 Tushare API调用次数: {stats['tushare_api_calls']}")
    logger.info(f"❌ 错误次数: {stats['errors']}")
    logger.info(f"🚫 频率限制触发次数: {stats['rate_limit_hits']}")
    with qmtLock:
        uniqueN = len(qmtState.get("uniqueCodes") or ())
        allowN = len(qmtState.get("allowSet") or ())
        dirtyN = len(qmtState.get("dirty") or ())
        sanity = qmtState.get("sanityOk")
        reason = qmtState.get("fallbackReason") or ""
    logger.info(
        "QMT 主源 enabled=%s healthy=%s sanity=%s unique=%d allow=%d dirty=%d "
        "silence_s=%.1f fallback=%s ticks=%d writes=%d",
        qmtState.get("enabled"),
        isQmtHealthy(),
        sanity,
        uniqueN,
        allowN,
        dirtyN,
        qmtSilenceSec(),
        reason or "-",
        stats.get("qmt_ticks") or 0,
        stats.get("qmt_writes") or 0,
    )
    
    # 显示轮询配置
    logger.info(f"🎯 轮询配置: {len(STOCK_PATTERNS)} 个普通市场模式 + {len(SPECIAL_TOPICS)} 个特殊主题")
    for pattern in STOCK_PATTERNS:
        logger.info(f"   - {pattern}")
    for topic in SPECIAL_TOPICS:
        logger.info(f"   - {topic['name']} ({topic['ts_code']}, topic={topic['topic']})")
    
    with cache_lock:
        if not memory_cache.empty:
            logger.info(f"💾 内存缓存: {len(memory_cache)}条记录")
            if stats['last_update_time']:
                last_update_str = datetime.fromtimestamp(stats['last_update_time']).strftime('%H:%M:%S')
                logger.info(f"🕐 最后数据更新: {last_update_str}")
        else:
            logger.warning("⚠️  内存缓存为空")
    
    current_calls = time_manager.get_current_calls()
    logger.info(f"🌐 rt_k API 状态: 当前窗口内 {current_calls}/50 次")
    if ENABLE_RT_IDX_K:
        idx_calls = time_manager_idx.get_current_calls()
        logger.info(f"🌐 rt_idx_k API 状态: 当前窗口内 {idx_calls}/50 次，累计调用 {stats['tushare_idx_api_calls']}，写入 {stats['idx_db_writes']} 条")
    if ENABLE_RT_SW_K:
        sw_calls = time_manager_sw.get_current_calls()
        logger.info(f"🌐 rt_sw_k API 状态: 当前窗口内 {sw_calls}/50 次，累计调用 {stats['tushare_sw_api_calls']}，写入 {stats['sw_db_writes']} 条")

    if connection_pool:
        try:
            test_conn = connection_pool.get_connection()
            test_conn.close()
            logger.info("✅ 数据库连接正常")
        except Exception as e:
            logger.error(f"❌ 数据库连接异常: {e}")
    else:
        logger.error("❌ 数据库连接池未初始化")
    
    if pro:
        logger.info("✅ Tushare API连接正常")
    else:
        logger.error("❌ Tushare API未初始化")
    
    if buffer_pool_monitor:
        summary = buffer_pool_monitor.get_performance_summary()
        logger.info(f"💾 Buffer Pool状态: 平均命中率 {summary['avg_hit_rate']:.2f}%，平均使用率 {summary['avg_used_percent']:.2f}%，总记录数 {summary['records_count']}，当前大小 {summary['buffer_pool_size_mb']:.2f}MB")
    else:
        logger.warning("⚠️  Buffer Pool监控器未初始化")
    
    logger.info("=" * 60)

# 更新统计
def update_stats(stat_type, value=1):
    """更新统计信息"""
    with stats_lock:
        if stat_type in stats:
            if stat_type == "last_update_time":
                stats[stat_type] = value
            else:
                stats[stat_type] += value


def _run_rt_k_cycle(forceAShare: bool = False) -> None:
    """单轮 rt_k：拉取 -> 处理 -> 写库。forceAShare 用于启动垫层和 9:10 清表后回填。"""
    if not running:
        return
    includeAShare = True if forceAShare else shouldFetchTushareAShare()
    data_df = fetch_realtime_stock_data(includeStockPatterns=includeAShare)
    if data_df is None or data_df.empty:
        logger.info("本轮回调 rt_k 未获取到数据（API 未返回或返回空，可能非交易时段或限频）")
        return
    process_realtime_data(data_df, skipCacheNewerFilter=forceAShare)


def _run_rt_idx_k_cycle() -> None:
    """单轮 rt_idx_k：拉取 -> 写库（线程内执行）。"""
    if not (running and ENABLE_RT_IDX_K and connection_pool_tushare):
        return
    idx_df = fetch_realtime_idx_data()
    if idx_df is not None and not idx_df.empty:
        save_rt_idx_k_batch(idx_df)


def _run_rt_sw_k_cycle() -> None:
    """单轮 rt_sw_k：拉取 -> 写库（线程内执行）。"""
    if not (running and ENABLE_RT_SW_K and connection_pool_tushare):
        return
    sw_df = fetch_realtime_sw_data()
    if sw_df is not None and not sw_df.empty:
        save_rt_sw_k_batch(sw_df)

# 清理资源
def cleanup():
    """清理资源"""
    global running, _instanceLockFile
    running = False
    _qmtStop.set()
    
    try:
        logger.info("正在清理资源...")
        releaseTusharertSingleton()
        if connection_pool:
            logger.info("正在关闭数据库连接池...")
        logger.info("资源清理完成")
    except Exception as e:
        logger.error(f"清理过程中发生错误: {e}")

# 主程序
if __name__ == "__main__":
    try:
        print("程序开始执行...")
        logger.info("🚀 Tushare实时个股行情程序开始执行...")
        
        stats['start_time'] = time.time()
        if not acquireTusharertSingleton():
            sys.exit(1)
        
        if not pro:
            logger.error("❌ Tushare API初始化失败，程序退出")
            sys.exit(1)
        
        logger.info("✅ Tushare API连接成功")
        
        if not connection_pool:
            logger.error("❌ 数据库连接池(stock_wind)初始化失败，程序退出")
            sys.exit(1)
        logger.info("✅ 数据库(stock_wind)连接成功")
        if ENABLE_RT_IDX_K and not connection_pool_tushare:
            logger.warning("⚠️ 未启用 stock_tushare 连接池，rt_idx_k / rt_sw_k 功能已跳过")

        if connection_pool_tushare:
            if ENABLE_RT_IDX_K:
                create_rt_idx_k_table_if_not_exists()
            if ENABLE_RT_SW_K:
                create_rt_sw_k_table_if_not_exists()
        
        now_str = datetime.now().strftime("%H:%M:%S")
        in_window = _is_polling_time_910_1510()
        logger.info(
            "🎯 程序启动完成。当前时间 %s，轮询窗口 9:10～15:10，%s；每次执行脚本都会先拉一次数据写表，再进入主循环",
            now_str, "当前在窗口内" if in_window else "当前在窗口外"
        )
        logger.info("按 Ctrl+C 可以优雅退出程序")
        logger.info(
            "轮询时间窗口：%s～%s（含），窗口外休眠 %d 秒",
            POLLING_WINDOW_START,
            POLLING_WINDOW_END,
            SLEEP_NON_TRADING_SEC,
        )
        logger.info("窗口内策略：rt_k、rt_idx_k、rt_sw_k 并行；命中限频时智能等待到下一次可调用")
        logger.info(
            "个股主源 QMT PUB %s enabled=%s；指数/申万/基金主题仍走 Tushare",
            QMT_PUSH_ADDR, RT_K_QMT_PRIMARY,
        )
        startQmtThreads()
        
        executor = ThreadPoolExecutor(max_workers=3)
        # 每次执行脚本（每次启动进程）都先拉一次数据写入表，再进入主循环
        logger.info("📥 本次执行脚本：先拉取一次数据并写入表...")
        startup_futures = [
            executor.submit(_run_rt_k_cycle, True),
            executor.submit(_run_rt_idx_k_cycle),
            executor.submit(_run_rt_sw_k_cycle),
        ]
        for f in startup_futures:
            try:
                f.result(timeout=120)
            except Exception as e:
                logger.error("启动时拉数异常: %s", e, exc_info=True)
        logger.info("✅ 启动时拉数写表完成，进入主循环")
        setQmtPaused(False)
        
        last_status_display = time.time()
        status_display_interval = 300  # 5分钟显示一次状态
        # 启动时已在窗口内并完成垫层：不再立刻 TRUNCATE（盘中重启会把 QMT 已写行清空）。
        # 窗口外启动则仍在真正进入 9:10 时清表一次。
        alreadyInWindow = _is_polling_time_910_1510()
        rt_k_cleared_in_window = alreadyInWindow
        rt_idx_k_cleared_in_window = alreadyInWindow
        rt_sw_k_cleared_in_window = alreadyInWindow
        
        while running:
            try:
                # 窗口外不拉数，只休眠，保留表内数据
                if not _is_polling_time_910_1510():
                    _sleep_interruptible(SLEEP_NON_TRADING_SEC)
                    continue

                # 进入 9:10 轮询窗口时清空一次 rt_k / rt_idx_k，其余时间只拉数、不清空
                forceRtKAShare = False
                if not rt_k_cleared_in_window:
                    logger.info("🗑️ 进入轮询窗口（9:10），清空 rt_k 表一次，再拉取数据源...")
                    setQmtPaused(True)
                    if not clear_rt_k_table():
                        logger.error("❌ 清空 rt_k 表失败，本轮回过，下轮再试")
                        setQmtPaused(False)
                        _sleep_interruptible(5)
                        continue
                    rt_k_cleared_in_window = True
                    forceRtKAShare = True
                if ENABLE_RT_IDX_K and connection_pool_tushare and not rt_idx_k_cleared_in_window:
                    logger.info("🗑️ 进入轮询窗口（9:10），清空 rt_idx_k 表一次，再拉取数据源...")
                    if not clear_rt_idx_k_table():
                        logger.error("❌ 清空 rt_idx_k 表失败，本轮回过，下轮再试")
                        if forceRtKAShare:
                            setQmtPaused(False)
                        _sleep_interruptible(5)
                        continue
                    rt_idx_k_cleared_in_window = True
                if ENABLE_RT_SW_K and connection_pool_tushare and not rt_sw_k_cleared_in_window:
                    logger.info("🗑️ 进入轮询窗口（9:10），清空 rt_sw_k 表一次，再拉取数据源...")
                    if not clear_rt_sw_k_table():
                        logger.error("❌ 清空 rt_sw_k 表失败，本轮回过，下轮再试")
                        if forceRtKAShare:
                            setQmtPaused(False)
                        _sleep_interruptible(5)
                        continue
                    rt_sw_k_cleared_in_window = True

                # 窗口内每轮拉取一次数据源并写表
                futures = [
                    executor.submit(_run_rt_k_cycle, forceRtKAShare),
                    executor.submit(_run_rt_idx_k_cycle),
                    executor.submit(_run_rt_sw_k_cycle),
                ]
                pending = set(futures)
                while pending and running:
                    done, pending = wait(pending, timeout=0.5)
                if not running:
                    break
                try:
                    futures[0].result(timeout=0)
                except Exception as e:
                    logger.error("rt_k 本轮回调异常: %s", e, exc_info=True)
                try:
                    futures[1].result(timeout=0)
                except Exception as e:
                    logger.error("rt_idx_k 本轮回调异常: %s", e, exc_info=True)
                try:
                    futures[2].result(timeout=0)
                except Exception as e:
                    logger.error("rt_sw_k 本轮回调异常: %s", e, exc_info=True)
                if forceRtKAShare:
                    setQmtPaused(False)
                
                # 显示状态
                current_time = time.time()
                if current_time - last_status_display > status_display_interval:
                    show_running_status()
                    last_status_display = current_time
                    
                    # Buffer Pool性能检查
                    if buffer_pool_monitor:
                        logger.info("=== Buffer Pool性能检查 ===")
                        buffer_pool_monitor.check_performance()
                
                _sleep_interruptible(TUSHARE_CONFIG['polling_interval'])
                
            except Exception as e:
                logger.error(f"主循环中发生错误: {e}")
                update_stats('errors')
                _sleep_interruptible(5)
            
    except KeyboardInterrupt:
        logger.info("接收到键盘中断信号")
    except Exception as e:
        logger.error(f"程序运行过程中发生错误: {e}")
        update_stats('errors')
        import traceback
        logger.error(f"详细错误信息: {traceback.format_exc()}")
    finally:
        logger.info("🔄 程序即将退出，显示最终运行状态...")
        show_running_status()
        
        try:
            executor.shutdown(wait=False)
        except Exception:
            pass
        cleanup()
        logger.info("✅ 程序已安全退出")
        sys.exit(0)

# 代码更新时间：2026-06-03 15:41（修复 memory_cache.update 时 int64/float64 混用导致 Invalid value for dtype int64）
# 代码更新时间：2026-05-14 16:05（并入 stock_tushare.rt_sw_k：建表/清空/并行拉取写入，ENABLE_RT_SW_K 开关）
# 原：2026-03-19（轮询窗口改为 9:10～15:10）
# 
# 主要功能：
# 1. 专门负责从Tushare API获取实时个股行情数据
# 2. 将原始数据存储到rt_k表的对应字段
# 3. 使用通配符方式批量获取数据，支持全市场监控
# 4. 简化的时间控制机制，统一0.6秒轮询间隔
# 5. 支持批量数据处理和数据库操作
# 6. 包含完整的错误处理和资源清理机制
# 7. 确保用户能够正常中断程序（Ctrl+C）
# 8. 生产环境就绪，支持优雅退出和异常恢复
# 9. 简化的API调用频率控制
# 10. Buffer Pool性能监控：监控命中率、使用率，低于阈值时告警
# 11. 连接池优化：增加连接数，支持更高并发访问
# 12. 死锁处理：增加重试机制，提高数据写入稳定性
# 13. 特殊主题支持：HQ_FND_TICK 沪/深基金主题，与普通市场数据统一存储；同码以基金主题为准
# 14. 新增trade_time字段：记录行情时间（格式YYYY-MM-DD HH:MM:SS），含格式验证和空值处理
#
# V2.1 优化内容（2026-02-11）：
# 1. 日志轮转：按天切割，保留3天；Windows 下使用 WindowsSafeTimedRotatingFileHandler 避免轮转时 PermissionError
# 2. 消除双重验证：删除sync_db_write_batch中的逐行验证（与validate_and_clean_data重复），写入速度提升3-5倍
# 3. 向量化构建batch_data：用itertuples替代iterrows双重循环，数据构建效率提升10-50倍
# 4. 内存缓存向量化更新：用update()+concat替代逐行iterrows，缓存更新速度提升10-50倍
# 5. 修复price_min=0.01篡改合法0值问题：停牌/集合竞价前bid_price等为0是正常的
# 6. 修复tushare_api_calls统计Bug：原来统计值永远为0，现在正确累计
# 7. 清理BufferPoolMonitor假优化：删除MySQL 8.x中不支持SESSION级修改的只读变量设置
# 8. 清理sync_db_write_batch假优化：删除innodb_doublewrite/innodb_flush_method无效SET语句
# 9. 进入轮询窗口（9:10）时清空 rt_k 表一次：确保启动轮询时表有数据，解决退市股/更名股残留
# 代码更新时间：2026-09-02 09:28 rt_sw_k 丢弃 close<=0 盘前脏截面
# 代码更新时间：2026-09-07 14:31 个股 QMT 15574 主源，Tushare 通配降为静默回补；基金/指数/申万不变
# 代码更新时间：2026-09-07 15:06 识别「频率超限(50次/分钟)」并整轮停打；本地窗口记满防连撞
# 代码更新时间：2026-09-07 17:06 单实例锁改 LOCALAPPDATA + 命名互斥体；兼容旧 TEMP 锁
# 代码更新时间：2026-09-07 17:12 1.2s 间隔从开始发请求起算，完成后不再额外再等
# 代码更新时间：2026-09-07 17:19 限频间隔自适应：撞限 +0.05s 至 1.5s，连续成功满窗再收回
# 代码更新时间：2026-09-07 17:27 窗口满时等待后仍打当前请求，不再 continue 跳过
# 代码更新时间：2026-09-07 17:33 窗口满最多短等一档间隔，避免 60s 冷却堵住并行接口
# 代码更新时间：2026-09-07 17:40 单实例以互斥体+LOCALAPPDATA 为准；TEMP 旧锁不再误拦重启
# 代码更新时间：2026-09-08 15:11 _sleep_interruptible 防 sleep 负数竞态；改用 monotonic
# 代码更新时间：2026-09-09 07:28 清 rt_idx_k 保留 TQ 两行 932000.CSI/899050.BJ
# 代码更新时间：2026-10-04 18:38 Windows 安全轮转类改为从 windows_safe_log 共享导入（实现逐字等价）
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
