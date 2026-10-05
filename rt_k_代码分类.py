# -*- coding: utf-8 -*-
"""
rt_k表 ts_code 分类脚本
功能：对 stock_wind.rt_k 表中的 ts_code 进行自动分类，建立分类映射表和视图
分类体系：A股(主板/科创板/创业板/北交所) | B股 | ETF | 债券ETF | LOF | REITs | 可转债 | 异常数据
"""

import os
import pymysql
import re
import logging
import signal
import sys
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler

# ============================================================
# 日志配置
# ============================================================
LOG_FILE = 'rt_k_代码分类.log'
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = TimedRotatingFileHandler(
    LOG_FILE, when='D', interval=1, backupCount=3, encoding='utf-8'
)
handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(handler)

# 控制台输出
console = logging.StreamHandler()
console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(console)

# ============================================================
# 用户中断处理（仅当作为主程序运行时注册，被其他模块引用时不覆盖信号）
# ============================================================
def signal_handler(sig, frame):
    logger.warning("用户中断，程序退出")
    sys.exit(0)

# ============================================================
# 数据库连接配置（支持环境变量，与 web_dashboard 等保持一致）
# ============================================================
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', ""),
    'port': int(os.getenv('DB_PORT', '3306')),
    'database': os.getenv('DB_NAME', 'stock_wind'),
    'charset': 'utf8mb4',
}

# ============================================================
# ts_code 分类规则定义
# ============================================================
# 格式: (正则表达式, 大类, 小类, 说明)
# 注意：规则按顺序匹配，先匹配到的优先
CLASSIFY_RULES = [
    # ======================== A股股票 ========================
    # 上海主板 (600/601/603/605)
    (r'^(600|601|603|605)\d{3}\.SH$', 'A股', 'SH主板', '上海主板A股'),
    # 科创板 (688/689)
    (r'^(688|689)\d{3}\.SH$', 'A股', 'SH科创板', '上海科创板'),
    # 深圳主板 (000/001)
    (r'^(000|001)\d{3}\.SZ$', 'A股', 'SZ主板', '深圳主板A股'),
    # 中小板 (002/003)，已并入深圳主板，但代码段独立
    (r'^(002|003)\d{3}\.SZ$', 'A股', 'SZ中小板', '深圳中小板(已并入主板)'),
    # 创业板 (300/301/302)
    (r'^(300|301|302)\d{3}\.SZ$', 'A股', 'SZ创业板', '深圳创业板'),
    # 北交所 (4xxxxx/8xxxxx/920xxx)
    (r'^[48]\d{5}\.BJ$', 'A股', 'BJ北交所', '北交所(原新三板精选层)'),
    (r'^920\d{3}\.BJ$', 'A股', 'BJ北交所', '北交所(专精特新号段)'),

    # ======================== B股 ========================
    (r'^900\d{3}\.SH$', 'B股', 'SH_B股', '上海B股'),
    (r'^20[01]\d{3}\.SZ$', 'B股', 'SZ_B股', '深圳B股'),

    # ======================== 可转债 ========================
    (r'^(123|127|128)\d{3}\.SZ$', '可转债', 'SZ可转债', '深圳可转债'),
    (r'^(110|111|113|118)\d{3}\.SH$', '可转债', 'SH可转债', '上海可转债'),

    # ======================== ETF基金 ========================
    # 深圳ETF
    (r'^159\d{3}\.SZ$', 'ETF', 'SZ_ETF', '深圳ETF'),
    # 上海权益类ETF (覆盖所有主要号段)
    (r'^(510|512|513|515|516|517|518|520)\d{3}\.SH$', 'ETF', 'SH_ETF', '上海ETF'),
    # 上海增强/指数基金型ETF
    (r'^(530|560|561|562|563)\d{3}\.SH$', 'ETF', 'SH_ETF增强', '上海增强型/指数ETF'),
    # 科创板ETF
    (r'^(588|589)\d{3}\.SH$', 'ETF', 'SH科创ETF', '上海科创板ETF'),
    # 跨市场在深交所上市的ETF（特殊情况，按名称含ETF判断）
    (r'^520\d{3}\.SZ$', 'ETF', 'SZ跨市场ETF', '深交所跨市场ETF'),
    (r'^560\d{3}\.SZ$', 'ETF', 'SZ跨市场ETF', '深交所跨市场ETF'),

    # ======================== 债券ETF ========================
    (r'^511\d{3}\.SH$', '债券ETF', 'SH债券ETF', '上海债券ETF'),
    (r'^551\d{3}\.SH$', '债券ETF', 'SH科创债ETF', '上海科创债ETF'),

    # ======================== LOF基金 ========================
    (r'^16[0-9]\d{3}\.SZ$', 'LOF', 'SZ_LOF', '深圳LOF基金'),
    (r'^(501|502)\d{3}\.SH$', 'LOF', 'SH_LOF', '上海LOF基金'),

    # ======================== 封闭式基金 ========================
    (r'^506\d{3}\.SH$', '封基', 'SH科创封基', '上海科创板封闭式基金'),

    # ======================== REITs ========================
    (r'^180\d{3}\.SZ$', 'REITs', 'SZ_REITs', '深圳REITs'),
    (r'^508\d{3}\.SH$', 'REITs', 'SH_REITs', '上海REITs'),

    # ======================== 债券(非ETF) ========================
    (r'^16\d{4}\.SH$', '债券', 'SH债券', '上海上市债券'),
]


def classify_ts_code(ts_code: str) -> tuple:
    """
    根据 ts_code 匹配分类规则
    返回: (大类, 小类, 说明)
    """
    for pattern, major, minor, desc in CLASSIFY_RULES:
        if re.match(pattern, ts_code):
            return major, minor, desc
    return '异常数据', '未分类', f'无法识别的代码: {ts_code}'


def get_connection():
    """获取数据库连接"""
    return pymysql.connect(**DB_CONFIG)


def create_category_table(cursor):
    """
    创建分类映射表 rt_k_code_category
    如已存在则先清空（因为每次运行都是全量刷新）
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rt_k_code_category (
            ts_code     VARCHAR(20) NOT NULL PRIMARY KEY COMMENT '证券代码',
            name        VARCHAR(100) COMMENT '证券名称',
            major_type  VARCHAR(20) NOT NULL COMMENT '大类(A股/B股/ETF/可转债/LOF/REITs/债券ETF/封基/债券/异常数据)',
            minor_type  VARCHAR(30) NOT NULL COMMENT '小类(SH主板/SZ创业板/SZ_ETF等)',
            description VARCHAR(100) COMMENT '分类说明',
            exchange    VARCHAR(5) COMMENT '交易所(SH/SZ/BJ)',
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        COMMENT='rt_k表ts_code分类映射表';
    """)
    logger.info("分类映射表 rt_k_code_category 已就绪")


def create_category_view(cursor):
    """
    创建实时行情分类视图 v_rt_k_with_category
    将 rt_k 和分类表 JOIN，后续统计分析可直接查询此视图
    """
    cursor.execute("DROP VIEW IF EXISTS v_rt_k_with_category;")
    cursor.execute("""
        CREATE VIEW v_rt_k_with_category AS
        SELECT 
            k.*,
            c.major_type,
            c.minor_type,
            c.description AS category_desc,
            c.exchange,
            -- 常用计算字段
            CASE WHEN k.pre_close > 0 
                THEN ROUND((k.close - k.pre_close) / k.pre_close * 100, 2) 
                ELSE NULL 
            END AS pct_change,
            CASE WHEN k.pre_close > 0 
                THEN ROUND((k.high - k.pre_close) / k.pre_close * 100, 2) 
                ELSE NULL 
            END AS high_pct,
            CASE WHEN k.pre_close > 0 
                THEN ROUND((k.low - k.pre_close) / k.pre_close * 100, 2) 
                ELSE NULL 
            END AS low_pct,
            CASE WHEN k.high > k.low AND k.high > 0
                THEN ROUND((k.high - k.low) / k.pre_close * 100, 2) 
                ELSE NULL 
            END AS amplitude
        FROM rt_k k
        LEFT JOIN rt_k_code_category c ON k.ts_code = c.ts_code;
    """)
    logger.info("实时行情分类视图 v_rt_k_with_category 创建成功")


def run_classification(silent=False):
    """
    执行分类主流程。
    silent: 为 True 时（如被 web 引用）不打印统计报告和视图验证，仅写库与写日志。
    """
    conn = get_connection()
    cursor = conn.cursor()

    try:
        # 1. 创建分类表
        create_category_table(cursor)
        conn.commit()

        # 2. 从 rt_k 读取所有 ts_code 和 name
        cursor.execute("SELECT ts_code, name FROM rt_k;")
        rows = cursor.fetchall()
        total = len(rows)
        logger.info(f"从 rt_k 表读取到 {total} 条记录，开始分类...")

        # 3. 逐条分类
        insert_data = []
        stats = {}  # 统计 {(大类, 小类): count}
        abnormal_list = []  # 异常数据记录

        for ts_code, name in rows:
            major, minor, desc = classify_ts_code(ts_code)

            # 提取交易所后缀
            if '.' in ts_code:
                exchange = ts_code.split('.')[-1]
            else:
                exchange = 'UNKNOWN'

            insert_data.append((ts_code, name, major, minor, desc, exchange))

            key = (major, minor)
            stats[key] = stats.get(key, 0) + 1

            if major == '异常数据':
                abnormal_list.append((ts_code, name))

        # 4. 批量写入分类表（先清空再插入，全量刷新）
        cursor.execute("TRUNCATE TABLE rt_k_code_category;")
        cursor.executemany("""
            INSERT INTO rt_k_code_category (ts_code, name, major_type, minor_type, description, exchange)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, insert_data)
        conn.commit()
        logger.info(f"分类数据写入完成，共 {len(insert_data)} 条")

        # 5. 创建视图
        create_category_view(cursor)
        conn.commit()

        if not silent:
            # 6. 输出分类统计报告
            print_classification_report(stats, total, abnormal_list)
            # 7. 验证视图可用性
            verify_view(cursor)

    except Exception as e:
        logger.error(f"分类过程出错: {e}", exc_info=True)
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def print_classification_report(stats: dict, total: int, abnormal_list: list):
    """打印分类统计报告"""
    print("\n" + "=" * 70)
    print(f"{'rt_k 表 ts_code 分类统计报告':^60}")
    print(f"{'总记录数: ' + str(total):^60}")
    print("=" * 70)

    # 按大类汇总
    major_stats = {}
    for (major, minor), cnt in sorted(stats.items()):
        if major not in major_stats:
            major_stats[major] = {'total': 0, 'details': []}
        major_stats[major]['total'] += cnt
        major_stats[major]['details'].append((minor, cnt))

    # 定义输出顺序
    order = ['A股', 'B股', '可转债', 'ETF', '债券ETF', 'LOF', '封基', 'REITs', '债券', '异常数据']

    for major in order:
        if major not in major_stats:
            continue
        info = major_stats[major]
        pct = info['total'] / total * 100
        print(f"\n[+] {major} (共 {info['total']} 只, 占比 {pct:.1f}%)")
        for minor, cnt in sorted(info['details'], key=lambda x: -x[1]):
            print(f"    - {minor:<20s} {cnt:>5d} 只")
        print(f"{'- ' * 30}")

    # 异常数据详情
    if abnormal_list:
        print(f"\n[!] 异常/无法识别的代码 ({len(abnormal_list)} 条):")
        for code, name in abnormal_list:
            print(f"   {code:<20s} {name}")

    print("\n" + "=" * 70)
    logger.info("分类统计报告输出完成")


def verify_view(cursor):
    """验证视图可用性，输出各大类的行情概览"""
    print("\n" + "=" * 70)
    print(f"{'视图 v_rt_k_with_category 验证 - 各大类行情概览':^60}")
    print("=" * 70)

    cursor.execute("""
        SELECT 
            major_type,
            COUNT(*) as cnt,
            SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) as up_cnt,
            SUM(CASE WHEN pct_change < 0 THEN 1 ELSE 0 END) as down_cnt,
            SUM(CASE WHEN pct_change = 0 THEN 1 ELSE 0 END) as flat_cnt,
            ROUND(AVG(pct_change), 2) as avg_pct,
            MAX(pct_change) as max_pct,
            MIN(pct_change) as min_pct
        FROM v_rt_k_with_category
        WHERE major_type IS NOT NULL AND major_type != '异常数据'
        GROUP BY major_type
        ORDER BY cnt DESC;
    """)
    results = cursor.fetchall()

    print(f"\n{'大类':<10s} {'数量':>6s} {'上涨':>6s} {'下跌':>6s} {'平盘':>6s} {'均涨幅%':>8s} {'最大%':>8s} {'最小%':>8s}")
    print("-" * 70)
    for row in results:
        major, cnt, up, down, flat, avg_pct, max_pct, min_pct = row
        up = up or 0
        down = down or 0
        flat = flat or 0
        avg_pct = avg_pct or 0
        max_pct = max_pct or 0
        min_pct = min_pct or 0
        print(f"{major:<10s} {cnt:>6d} {int(up):>6d} {int(down):>6d} {int(flat):>6d} {float(avg_pct):>8.2f} {float(max_pct):>8.2f} {float(min_pct):>8.2f}")

    print("\n[OK] 视图验证通过，可直接用于后续统计分析")
    print("\n常用查询示例:")
    print("  -- 查询所有A股实时行情（含涨跌幅）")
    print("  SELECT * FROM v_rt_k_with_category WHERE major_type = 'A股';")
    print("")
    print("  -- 按板块统计涨跌家数")
    print("  SELECT minor_type, COUNT(*) as cnt,")
    print("         SUM(pct_change>0) as up, SUM(pct_change<0) as down")
    print("  FROM v_rt_k_with_category WHERE major_type = 'A股'")
    print("  GROUP BY minor_type;")
    print("")
    print("  -- 查询涨幅前10的ETF")
    print("  SELECT ts_code, name, pct_change, amplitude")
    print("  FROM v_rt_k_with_category WHERE major_type = 'ETF'")
    print("  ORDER BY pct_change DESC LIMIT 10;")
    print("")
    print("  -- 查询特定板块（如科创板）涨停股")
    print("  SELECT ts_code, name, pct_change")
    print("  FROM v_rt_k_with_category")
    print("  WHERE minor_type = 'SH科创板' AND pct_change >= 19.9;")


if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("=" * 50)
    logger.info("开始执行 rt_k ts_code 分类")
    logger.info("=" * 50)

    run_classification()

    logger.info("分类任务全部完成")

# 代码更新时间: 2026-03-05 (支持 silent 模式与仅主程序注册 signal，便于被 web_dashboard 引用)
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
