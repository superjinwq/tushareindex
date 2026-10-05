# -*- coding: utf-8 -*-
"""
rt_monitor 依赖表的数据新鲜度 / 覆盖面体检（只读，不改任何数据）。

检查项（按表类型）：
  daily    日线：最新日期 vs 最近交易日（trade_cal）滞后几个交易日；最新日行数 vs 前 N 个交易日中位数（可按市场拆分）；关键列空值
  minute   分钟：同上，按自然日聚合
  today    盘中当日表：只看日期是否为最近交易日
  snapshot 实时快照：快照日期、陈旧行数；rt_k 对照 stock_basic 按 SH/SZ/BJ 计算覆盖率
  period   财报/股东：按法定披露截止日推算应有的最新报告期，覆盖股票数 vs 去年同期；最新公告日滞后
  custom   参考表：交易日历覆盖未来、期权未到期合约、申万成分重复/缺失、指数权重月度更新等

性能与安全：单连接串行、会话只读、每条 SELECT 最长 30s（MAX_EXECUTION_TIME）；
大表只查最近若干交易日，且只走索引（无日期前导索引的表用 GROUP BY ts_code 松散索引扫描 / SKIP_SCAN）。

运行：
  python data_health_check.py                # 全量体检，写 output/data_health/latest.json
  python data_health_check.py --only rt_k    # 只查一张表（调试）
  python data_health_check.py --days 10      # 行数对比窗口（交易日）
  双击 数据体检.bat                          # 同全量体检，窗口里显示易懂摘要并暂停
退出码：0 = 全部 OK 或仅有 WARN 提醒；1 = 有 FAIL/ERROR；130 = 用户中断。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import statistics
import sys
import time
from typing import Any, Callable

import pymysql

from windows_safe_log import WindowsSafeTimedRotatingFileHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, 'logs', 'data_health_check.log')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'output', 'data_health')
LATEST_JSON = os.path.join(OUTPUT_DIR, 'latest.json')
HISTORY_KEEP = 14
QUERY_TIMEOUT_MS = 30000

logger = logging.getLogger('data_health_check')

STATUS_RANK = {'OK': 0, 'INFO': 0, 'WARN': 1, 'FAIL': 2, 'ERROR': 2}

# 北交所 2025 年起统一换为 920 段新代码；stock_basic 中仍为 L、且与某个 92 段新代码同名的旧代码视为陈旧行
STALE_BJ_SQL = ("({a}.ts_code LIKE '%%.BJ' AND {a}.ts_code NOT LIKE '92%%' AND EXISTS (SELECT 1 FROM "
                "`stock_tushare`.`stock_basic` n WHERE n.ts_code LIKE '92%%' AND n.list_status='L' AND n.name={a}.name))")
# 财报类覆盖率基准：同一报告期利润表（income）出现、且报告期末前已上市、当前在市、非北交所陈旧旧代码的股票
# （income 中含已退市仍披露的公司与北交所新旧双代码，直接作分母会把缺失率抬高）
ANCHOR_SQL = ("SELECT DISTINCT i.ts_code FROM `stock_tushare`.`income` i JOIN `stock_tushare`.`stock_basic` b "
              "ON b.ts_code = i.ts_code WHERE i.end_date = %s AND b.list_status = 'L' AND b.list_date <= %s AND NOT "
              + STALE_BJ_SQL.format(a='b'))

# ---------------------------------------------------------------------------
# 体检清单：表名/列名均为脚本内常量（拼进 SQL 前不来自外部输入）
# latest: max=有日期前导索引直接 MAX；loose=GROUP BY ts_code 松散索引扫描
# count: group=日期前导索引按日分组；skip=逐日 COUNT + SKIP_SCAN；none=不比行数
# split: 分市场/分组表达式（如 RIGHT(ts_code,2)、exchange、underlying）
# ---------------------------------------------------------------------------
CHECKS: list[dict[str, Any]] = [
    # —— 日线 ——
    dict(schema='stock_tushare', table='stk_factor_pro', kind='daily', dateCol='trade_date', latest='max', count='group',
         split='RIGHT(ts_code,2)', nullCols=['close_qfq', 'vol'], page='fina/sw/niusan/首次新高'),
    dict(schema='stock_tushare', table='stk_factor_ma', kind='daily', dateCol='trade_date', latest='loose', count='skip',
         nullCols=['ma20'], page='fina/sw/牛散'),
    dict(schema='stock_tushare', table='etf_daily', kind='daily', dateCol='trade_date', latest='max', count='group',
         nullCols=['close'], page='ETF/期权'),
    dict(schema='stock_tushare', table='index_daily', kind='daily', dateCol='trade_date', latest='max', count='group',
         nullCols=['close'], page='指数/两融/市场状态'),
    dict(schema='stock_tushare', table='limit_list_ths', kind='daily', dateCol='trade_date', latest='max', count='none',
         page='主页涨跌停'),
    dict(schema='stock_tushare', table='marketinfo', kind='daily', dateCol='trade_date', latest='max', count='none',
         nullCols=['amount', 'up_num'], page='主页/市场状态'),
    dict(schema='stock_tushare', table='margin_daily', kind='daily', dateCol='trade_date', latest='max', count='none',
         allowLag=1, nullCols=['rzye_sum'], page='两融'),
    dict(schema='stock_wind', table='margin_detail', kind='daily', dateCol='trade_date', latest='max', count='group',
         split='RIGHT(ts_code,2)', allowLag=1, nullCols=['rzye'], page='申万/两融'),
    # 期权合约逐月到期，行数天然跳变：不比中位数，改为对照 opt_basic 存续合约的覆盖率
    dict(schema='stock_tushare', table='opt_daily', kind='daily', dateCol='trade_date', latest='max', count='none',
         optCoverage=True, nullCols=['close', 'settle'], page='期权T型/SP计划'),
    dict(schema='stock_tushare', table='opt_iv_panel', kind='daily', dateCol='trade_date', latest='max', count='group',
         split='underlying', nullCols=['representative_iv'], page='期权IV'),
    dict(schema='stock_tushare', table='etf_fund_adj', kind='daily', dateCol='trade_date', latest='max', count='group',
         nullCols=['adj_factor'], page='ETF前复权'),
    dict(schema='stock_tushare', table='limit_stats_archive_detail', kind='daily', dateCol='trade_date', latest='max',
         count='none', page='申万/主页涨跌停明细'),
    dict(schema='stock_tushare', table='stk_qfq_close_break', kind='daily', dateCol='scan_end_date', latest='max',
         count='none', page='fina首次新高'),
    # —— 分钟 / 盘中当日 ——
    dict(schema='stock_tushare', table='etf_stk_mins', kind='minute', dateCol='trade_time', latest='max', count='group',
         nullCols=['close'], page='ETF分钟'),
    dict(schema='stock_tushare', table='etf_stk_mins_ma', kind='minute', dateCol='trade_time', latest='max', count='group',
         page='ETF均线信号'),
    dict(schema='stock_tushare', table='idx_mins', kind='minute', dateCol='trade_time', latest='max', count='group',
         page='指数分钟'),
    dict(schema='stock_tushare', table='etf_rt_min', kind='today', dateCol='trade_time', latest='max', page='ETF盘中'),
    dict(schema='stock_tushare', table='rt_idx_min', kind='today', dateCol='trade_time', latest='max', page='指数盘中'),
    # —— 实时快照 ——
    dict(schema='stock_wind', table='rt_k', kind='snapshot', dateCol='trade_time', coverage=True, nullCols=['close'],
         page='主页/fina/牛散/申万'),
    dict(schema='stock_tushare', table='rt_idx_k', kind='snapshot', dateCol='trade_time', page='指数实时'),
    dict(schema='stock_tushare', table='rt_sw_k', kind='snapshot', dateCol='trade_time', page='申万实时'),
    dict(schema='stock_tushare', table='etf_bias_realtime', kind='snapshot', dateCol='trade_time', page='ETF BIAS'),
    # —— 财报 / 股东 ——
    dict(schema='stock_tushare', table='income', kind='period', periodSql='idx', annLatest='loose', isAnchor=True,
         nullCols=['n_income_attr_p'], page='fina财报'),
    dict(schema='stock_tushare', table='balancesheet', kind='period', periodSql='idx', annLatest='loose',
         nullCols=['total_assets'], page='fina财报'),
    dict(schema='stock_tushare', table='cashflow', kind='period', periodSql='idx', annLatest='loose',
         nullCols=['n_cashflow_act'], page='fina财报'),
    dict(schema='stock_tushare', table='fina_indicator', kind='period', periodSql='idx', annLatest='max',
         nullCols=['roe'], page='fina财报'),
    dict(schema='stock_tushare', table='fina_quarter_metrics', kind='period', periodSql='idx', annLatest='none',
         anchorExcludeIndustry=['银行', '证券', '保险', '多元金融'], page='fina财报'),
    dict(schema='stock_tushare', table='fina_mainbz', kind='period', periodSql='loose', annLatest='none',
         page='fina主营'),
    dict(schema='stock_tushare', table='top10_holders', kind='period', periodSql='loose', annLatest='loose',
         page='fina股东'),
    dict(schema='stock_tushare', table='top10_floatholders', kind='period', periodSql='ann', annLatest='loose',
         page='fina/牛散'),
    dict(schema='stock_tushare', table='stk_holdernumber', kind='period', periodSql='ann', annLatest='loose',
         page='fina股东人数'),
]


def setupLogger() -> None:
    # 仅单独运行时配置；被导入时不碰根日志器（LESSON-085）
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = WindowsSafeTimedRotatingFileHandler(LOG_FILE, when='D', interval=1, backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.handlers[:] = [fh, ch]
    logger.propagate = False


def getConn() -> pymysql.connections.Connection:
    dbPassword = (os.environ.get('DB_PASSWORD') or '').strip()
    if not dbPassword:
        raise SystemExit('缺少环境变量 DB_PASSWORD')
    conn = pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'), port=int(os.environ.get('DB_PORT', '3306')),
        user=os.environ.get('DB_USER', 'root'), password=dbPassword,
        charset='utf8mb4', connect_timeout=10, read_timeout=QUERY_TIMEOUT_MS // 1000 + 30, autocommit=True)
    with conn.cursor() as cur:
        cur.execute('SET SESSION TRANSACTION READ ONLY')
        cur.execute('SET SESSION MAX_EXECUTION_TIME = %s', (QUERY_TIMEOUT_MS,))
    return conn


class Db:
    """串行查询 + 累计每张表的查询耗时。"""

    def __init__(self, conn: pymysql.connections.Connection):
        self.conn = conn
        self.queryCount = 0

    def rows(self, sql: str, args: tuple | list | None = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            self.queryCount += 1
            return list(cur.fetchall())

    def one(self, sql: str, args: tuple | list | None = None) -> Any:
        r = self.rows(sql, args)
        return r[0][0] if r else None


def qn(schema: str, table: str) -> str:
    return f'`{schema}`.`{table}`'


def toDate(v: Any) -> dt.date | None:
    if v is None:
        return None
    return v.date() if isinstance(v, dt.datetime) else v


def worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: STATUS_RANK.get(s, 0)) if statuses else 'OK'


# ---------------------------------------------------------------------------
# 交易日上下文
# ---------------------------------------------------------------------------
def buildContext(db: Db, now: dt.datetime) -> dict[str, Any]:
    today = now.date()
    days = [toDate(r[0]) for r in db.rows(
        "SELECT cal_date FROM `stock_tushare`.`trade_cal` WHERE exchange='SSE' AND is_open=1 "
        "AND cal_date <= %s ORDER BY cal_date DESC LIMIT 80", (today,))]
    days.reverse()
    isOpenToday = bool(days) and days[-1] == today
    # 日线盘后才入库：开市日 18:00 前以前一交易日为期望最新日
    if isOpenToday and now.hour < 18:
        expectedDaily = days[-2]
    else:
        expectedDaily = days[-1]
    # 盘中表/快照：开市日 09:30 后期望当日
    if isOpenToday and (now.hour, now.minute) >= (9, 30):
        expectedIntraday = today
    else:
        expectedIntraday = days[-2] if isOpenToday else days[-1]
    return dict(now=now, today=today, tradeDays=days, isOpenToday=isOpenToday,
                expectedDaily=expectedDaily, expectedIntraday=expectedIntraday)


def lagTradeDays(latest: dt.date | None, expected: dt.date, tradeDays: list[dt.date]) -> int | None:
    if latest is None:
        return None
    return sum(1 for d in tradeDays if latest < d <= expected)


def lagStatus(lag: int | None, allowLag: int = 0) -> str:
    if lag is None:
        return 'FAIL'
    if lag <= allowLag:
        return 'OK'
    return 'WARN' if lag == allowLag + 1 else 'FAIL'


def ratioStatus(ratio: float | None, warnBelow: float = 0.9, failBelow: float = 0.7) -> str:
    if ratio is None:
        return 'INFO'
    if ratio < failBelow:
        return 'FAIL'
    return 'WARN' if ratio < warnBelow else 'OK'


def expectedPeriod(today: dt.date) -> dt.date:
    """法定披露截止日已过的最新报告期：一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30。"""
    y = today.year
    candidates = [
        (dt.date(y, 9, 30), dt.date(y, 10, 31)),
        (dt.date(y, 6, 30), dt.date(y, 8, 31)),
        (dt.date(y, 3, 31), dt.date(y, 4, 30)),
        (dt.date(y - 1, 12, 31), dt.date(y, 4, 30)),
        (dt.date(y - 1, 9, 30), dt.date(y - 1, 10, 31)),
    ]
    return max(p for p, deadline in candidates if deadline < today)


# ---------------------------------------------------------------------------
# 各类检查
# ---------------------------------------------------------------------------
def latestValue(db: Db, chk: dict, col: str, mode: str) -> Any:
    t = qn(chk['schema'], chk['table'])
    if mode == 'loose':
        return db.one(f'SELECT MAX(m) FROM (SELECT MAX(`{col}`) m FROM {t} GROUP BY ts_code) x')
    return db.one(f'SELECT MAX(`{col}`) FROM {t}')


def dailyCounts(db: Db, chk: dict, days: list[dt.date]) -> dict[dt.date, dict[str, int]]:
    """返回 {日期: {分组: 行数}}；无分组时分组键为 'ALL'。"""
    t, col, mode = qn(chk['schema'], chk['table']), chk['dateCol'], chk.get('count', 'group')
    out: dict[dt.date, dict[str, int]] = {d: {} for d in days}
    if mode == 'skip':
        for d in days:
            out[d]['ALL'] = int(db.one(
                f'SELECT /*+ SKIP_SCAN(x PRIMARY) */ COUNT(*) FROM {t} x WHERE `{col}` = %s', (d,)) or 0)
        return out
    split = chk.get('split')
    if chk['kind'] == 'minute':
        dateExpr, where, args = f'DATE(`{col}`)', f'`{col}` >= %s AND `{col}` < %s', (days[0], days[-1] + dt.timedelta(days=1))
    else:
        dateExpr, where, args = f'`{col}`', f'`{col}` BETWEEN %s AND %s', (days[0], days[-1])
    grp = f', {split}' if split else ''
    sel = f', {split}' if split else ", 'ALL'"
    for d, g, n in db.rows(f'SELECT {dateExpr} d{sel} g, COUNT(*) FROM {t} WHERE {where} GROUP BY d{grp}', args):
        d = toDate(d)
        if d in out:
            out[d][str(g)] = int(n)
    return out


def compareCounts(counts: dict[dt.date, dict[str, int]], latest: dt.date) -> tuple[str, dict]:
    """最新日各分组行数 vs 此前交易日中位数。"""
    hist = [d for d in counts if d < latest and counts[d]]
    groups = sorted({g for d in counts for g in counts[d]})
    detail, statuses = {}, []
    for g in groups:
        cur = counts.get(latest, {}).get(g, 0)
        past = [counts[d].get(g, 0) for d in hist]
        med = statistics.median(past) if past else None
        ratio = round(cur / med, 3) if med else None
        st = ratioStatus(ratio)
        detail[g] = dict(latest=cur, median=med, ratio=ratio, status=st)
        statuses.append(st)
    return worst(*statuses), detail


def nullCheck(db: Db, chk: dict, whereSql: str, args: tuple, nullMax: float = 0.01) -> tuple[str, dict]:
    cols = chk.get('nullCols') or []
    if not cols:
        return 'OK', {}
    t = qn(chk['schema'], chk['table'])
    sums = ', '.join(f'SUM(`{c}` IS NULL)' for c in cols)
    r = db.rows(f'SELECT COUNT(*), {sums} FROM {t} WHERE {whereSql}', args)[0]
    total = int(r[0] or 0)
    detail, statuses = {'rows': total}, []
    for c, n in zip(cols, r[1:]):
        n = int(n or 0)
        ratio = round(n / total, 4) if total else None
        st = 'WARN' if ratio and ratio > nullMax else 'OK'
        detail[c] = dict(nullRows=n, ratio=ratio, status=st)
        statuses.append(st)
    return worst(*statuses), detail


def nullCheckByCodes(db: Db, chk: dict, day: dt.date, batch: int = 1000) -> tuple[str, dict]:
    """主键 (ts_code, 日期) 且无日期前导索引的大表：先 SKIP_SCAN 取当日代码，再按主键分批点查，避免全表扫描。"""
    cols = chk.get('nullCols') or []
    if not cols:
        return 'OK', {}
    t, col = qn(chk['schema'], chk['table']), chk['dateCol']
    codes = [r[0] for r in db.rows(f'SELECT /*+ SKIP_SCAN(x PRIMARY) */ ts_code FROM {t} x WHERE `{col}` = %s', (day,))]
    total, nulls = 0, [0] * len(cols)
    sums = ', '.join(f'SUM(`{c}` IS NULL)' for c in cols)
    for i in range(0, len(codes), batch):
        part = codes[i:i + batch]
        marks = ','.join(['%s'] * len(part))
        r = db.rows(f'SELECT COUNT(*), {sums} FROM {t} WHERE ts_code IN ({marks}) AND `{col}` = %s', (*part, day))[0]
        total += int(r[0] or 0)
        nulls = [a + int(b or 0) for a, b in zip(nulls, r[1:])]
    detail, statuses = {'rows': total}, []
    for c, n in zip(cols, nulls):
        ratio = round(n / total, 4) if total else None
        st = 'WARN' if ratio and ratio > 0.01 else 'OK'
        detail[c] = dict(nullRows=n, ratio=ratio, status=st)
        statuses.append(st)
    return worst(*statuses), detail


def optContractCoverage(db: Db, day: dt.date) -> tuple[str, dict]:
    """当日存续合约（opt_basic 上市日 ≤ 当日 ≤ 摘牌日）在 opt_daily 中有行的比例，按交易所。"""
    rows = db.rows(
        'SELECT b.exchange, COUNT(*), SUM(d.ts_code IS NOT NULL) FROM `stock_tushare`.`opt_basic` b '
        'LEFT JOIN `stock_tushare`.`opt_daily` d ON d.ts_code = b.ts_code AND d.trade_date = %s '
        'WHERE b.list_date <= %s AND b.delist_date >= %s GROUP BY b.exchange', (day, day, day))
    detail, statuses = {}, []
    for ex, live, present in rows:
        live, present = int(live), int(present or 0)
        ratio = round(present / live, 4) if live else None
        st = ratioStatus(ratio, 0.98, 0.9)
        detail[ex] = dict(liveContracts=live, withDaily=present, ratio=ratio, status=st)
        statuses.append(st)
    return worst(*statuses), detail


def checkDaily(db: Db, chk: dict, ctx: dict, nDays: int) -> dict:
    latest = toDate(latestValue(db, chk, chk['dateCol'], chk.get('latest', 'max')))
    expected = ctx['expectedDaily']
    lag = lagTradeDays(latest, expected, ctx['tradeDays'])
    res = dict(latest=str(latest), expected=str(expected), lagTradeDays=lag)
    statuses = [lagStatus(lag, chk.get('allowLag', 0))]
    if latest is None:
        return dict(res, status='FAIL', issues=['表为空或无日期'])
    if chk.get('count', 'group') != 'none':
        window = [d for d in ctx['tradeDays'] if d <= latest][-(nDays + 1):]
        st, detail = compareCounts(dailyCounts(db, chk, window), latest)
        res['rowsVsMedian'] = detail
        statuses.append(st)
    if chk.get('optCoverage'):
        st, res['liveContractCoverage'] = optContractCoverage(db, latest)
        statuses.append(st)
    if chk.get('count') == 'skip':
        st, res['nulls'] = nullCheckByCodes(db, chk, latest)
    else:
        if chk['kind'] == 'minute':
            where, args = f"`{chk['dateCol']}` >= %s AND `{chk['dateCol']}` < %s", (latest, latest + dt.timedelta(days=1))
        else:
            where, args = f"`{chk['dateCol']}` = %s", (latest,)
        st, res['nulls'] = nullCheck(db, chk, where, args)
    statuses.append(st)
    return dict(res, status=worst(*statuses))


def checkToday(db: Db, chk: dict, ctx: dict, nDays: int) -> dict:
    latest = toDate(latestValue(db, chk, chk['dateCol'], 'max'))
    expected = ctx['expectedIntraday']
    lag = lagTradeDays(latest, expected, ctx['tradeDays'])
    rows = db.one(f"SELECT COUNT(*) FROM {qn(chk['schema'], chk['table'])}")
    return dict(latest=str(latest), expected=str(expected), lagTradeDays=lag, rows=int(rows or 0),
                status=lagStatus(lag))


def checkSnapshot(db: Db, chk: dict, ctx: dict, nDays: int) -> dict:
    t, col = qn(chk['schema'], chk['table']), chk['dateCol']
    latestTs = db.one(f'SELECT MAX(`{col}`) FROM {t}')
    latest = toDate(latestTs)
    expected = ctx['expectedIntraday']
    lag = lagTradeDays(latest, expected, ctx['tradeDays'])
    total = int(db.one(f'SELECT COUNT(*) FROM {t}') or 0)
    stale = int(db.one(f'SELECT COUNT(*) FROM {t} WHERE `{col}` < %s', (latest,)) or 0) if latest else total
    res = dict(latest=str(latestTs), expected=str(expected), lagTradeDays=lag, rows=total, staleRows=stale)
    statuses = [lagStatus(lag)]
    if chk.get('coverage'):
        st, res['coverage'] = snapshotCoverage(db, t, col, latest)
        statuses.append(st)
    if chk.get('nullCols') and latest:
        st, res['nulls'] = nullCheck(db, chk, f'`{col}` >= %s', (latest,))
        statuses.append(st)
    return dict(res, status=worst(*statuses))


def snapshotCoverage(db: Db, t: str, col: str, latest: dt.date | None) -> tuple[str, dict]:
    """在市 A 股（stock_basic L）在快照里出现且日期为最新日的比例，按 SH/SZ/BJ。"""
    rows = db.rows(
        f"SELECT RIGHT(b.ts_code,2) m, COUNT(*), SUM(k.ts_code IS NOT NULL), SUM(k.`{col}` >= %s) "
        f"FROM `stock_tushare`.`stock_basic` b LEFT JOIN {t} k ON k.ts_code = b.ts_code "
        f"WHERE b.list_status='L' AND NOT {STALE_BJ_SQL.format(a='b')} GROUP BY m", (latest,))
    detail, statuses = {}, []
    for m, listed, present, fresh in rows:
        listed, present, fresh = int(listed), int(present or 0), int(fresh or 0)
        ratio = round(fresh / listed, 4) if listed else None
        st = ratioStatus(ratio, 0.98, 0.9)
        detail[m] = dict(listed=listed, present=present, freshOnLatest=fresh, ratio=ratio, status=st)
        statuses.append(st)
    return worst(*statuses), detail


def periodCodes(db: Db, chk: dict, period: dt.date) -> int:
    t, mode = qn(chk['schema'], chk['table']), chk.get('periodSql', 'idx')
    if mode == 'idx':
        return int(db.one(f'SELECT COUNT(DISTINCT ts_code) FROM {t} WHERE end_date = %s', (period,)) or 0)
    if mode == 'ann':
        # 唯一键 (ts_code, ann_date, end_date, ...)：公告日必晚于报告期，用 ann_date 范围收窄
        return int(db.one(f'SELECT COUNT(DISTINCT ts_code) FROM {t} WHERE ann_date >= %s AND end_date = %s',
                          (period, period)) or 0)
    return int(db.one(f'SELECT COUNT(*) FROM (SELECT ts_code FROM {t} WHERE end_date = %s GROUP BY ts_code) x',
                      (period,)) or 0)


def anchorMissing(db: Db, chk: dict, period: dt.date) -> dict[str, int]:
    """同报告期利润表有、本表无的股票数，按市场。按 (ts_code, end_date) 逐只点查走唯一键前缀。"""
    t = qn(chk['schema'], chk['table'])
    anchorSql, anchorArgs = anchorQuery(chk, period)
    rows = db.rows(
        f'SELECT RIGHT(a.ts_code,2), COUNT(*) FROM ({anchorSql}) a '
        f'WHERE NOT EXISTS (SELECT 1 FROM {t} x WHERE x.ts_code = a.ts_code AND x.end_date = %s) GROUP BY 1',
        (*anchorArgs, period))
    return {m: int(n) for m, n in rows}


def anchorQuery(chk: dict, period: dt.date) -> tuple[str, tuple]:
    """基准股票集合 SQL；表可声明 anchorExcludeIndustry（如衍生表不算金融业）。"""
    excl = chk.get('anchorExcludeIndustry') or []
    if not excl:
        return ANCHOR_SQL, (period, period)
    marks = ','.join(['%s'] * len(excl))
    return ANCHOR_SQL + f' AND COALESCE(b.industry, \'\') NOT IN ({marks})', (period, period, *excl)


def checkPeriod(db: Db, chk: dict, ctx: dict, nDays: int) -> dict:
    """利润表自身看同比覆盖；其余表看「利润表有而本表缺」的比例（同比受北交所新旧双代码干扰，仅供参考）。"""
    period = expectedPeriod(ctx['today'])
    lastYear = period.replace(year=period.year - 1)
    cur, prev = periodCodes(db, chk, period), periodCodes(db, chk, lastYear)
    ratio = round(cur / prev, 3) if prev else None
    res = dict(expectedPeriod=str(period), codes=cur, codesSamePeriodLastYear=prev, ratio=ratio)
    if chk.get('isAnchor'):
        statuses = [ratioStatus(ratio, 0.95, 0.85)]
    else:
        anchorSql, anchorArgs = anchorQuery(chk, period)
        anchorN = int(db.one(f'SELECT COUNT(*) FROM ({anchorSql}) a', anchorArgs) or 0)
        missing = anchorMissing(db, chk, period)
        missRatio = round(sum(missing.values()) / anchorN, 4) if anchorN else None
        res.update(anchorCodes=anchorN, missingVsIncome=missing, missingRatio=missRatio)
        statuses = ['FAIL' if missRatio is None or missRatio > 0.10 else 'WARN' if missRatio > 0.02 else 'OK']
    if chk.get('annLatest', 'none') != 'none':
        annMax = toDate(latestValue(db, chk, 'ann_date', chk['annLatest']))
        lag = lagTradeDays(annMax, ctx['expectedDaily'], ctx['tradeDays'])
        res.update(latestAnnDate=str(annMax), annLagTradeDays=lag)
        statuses.append('OK' if lag is not None and lag <= 3 else 'WARN')
    if chk.get('nullCols'):
        st, res['nulls'] = nullCheck(db, chk, 'end_date = %s', (period,), nullMax=0.05)
        statuses.append(st)
    return dict(res, status=worst(*statuses))


# ---------------------------------------------------------------------------
# 参考表 / 重复检查（自定义）
# ---------------------------------------------------------------------------
def checkTradeCal(db: Db, ctx: dict) -> dict:
    maxDate = toDate(db.one("SELECT MAX(cal_date) FROM `stock_tushare`.`trade_cal` WHERE exchange='SSE'"))
    ahead = (maxDate - ctx['today']).days if maxDate else None
    return dict(maxCalDate=str(maxDate), daysAhead=ahead,
                status='OK' if ahead is not None and ahead >= 30 else 'FAIL')


def checkStockBasic(db: Db, ctx: dict) -> dict:
    rows = db.rows("SELECT RIGHT(ts_code,2), COUNT(*) FROM `stock_tushare`.`stock_basic` WHERE list_status='L' GROUP BY 1")
    upd = db.one('SELECT MAX(updated_at) FROM `stock_tushare`.`stock_basic`')
    ageDays = (ctx['now'] - upd).days if upd else None
    newListed = int(db.one("SELECT COUNT(*) FROM `stock_tushare`.`stock_basic` WHERE list_date > %s",
                           (ctx['expectedDaily'] - dt.timedelta(days=30),)) or 0)
    staleBj = int(db.one("SELECT COUNT(*) FROM `stock_tushare`.`stock_basic` b WHERE b.list_status='L' AND "
                         + STALE_BJ_SQL.format(a='b')) or 0)
    isFresh = ageDays is not None and ageDays <= 14
    return dict(listedByMarket={m: int(n) for m, n in rows}, lastUpdatedAt=str(upd), updatedAgeDays=ageDays,
                listedLast30d=newListed, staleBjOldCodesStillListed=staleBj,
                status='OK' if isFresh and not staleBj else 'WARN')


def checkSwMembers(db: Db, ctx: dict) -> dict:
    t = '`stock_tushare`.`index_member_all_sw`'
    dupRows = db.rows(f"SELECT COUNT(*), SUM(nL3 > 1) FROM (SELECT ts_code, COUNT(*) n, COUNT(DISTINCT l3_code) nL3 "
                      f"FROM {t} WHERE is_new='Y' AND out_date IS NULL GROUP BY ts_code HAVING n > 1) x")[0]
    dupCodes, dupDiffL3 = int(dupRows[0] or 0), int(dupRows[1] or 0)
    missing = db.rows(
        "SELECT RIGHT(b.ts_code,2), COUNT(*) FROM `stock_tushare`.`stock_basic` b "
        f"WHERE b.list_status='L' AND NOT {STALE_BJ_SQL.format(a='b')} AND NOT EXISTS "
        f"(SELECT 1 FROM {t} m WHERE m.ts_code=b.ts_code AND m.is_new='Y' AND m.out_date IS NULL) GROUP BY 1")
    missingMap = {m: int(n) for m, n in missing}
    # 页面按 in_date 最新取一条；只有「最新 in_date 上并列多个三级行业」才会让页面归属不确定
    ambiguous = [r[0] for r in db.rows(
        f"SELECT m.ts_code FROM {t} m JOIN (SELECT ts_code, MAX(in_date) d FROM {t} "
        f"WHERE is_new='Y' OR is_new IS NULL GROUP BY ts_code) x ON x.ts_code=m.ts_code AND x.d=m.in_date "
        f"WHERE (m.is_new='Y' OR m.is_new IS NULL) GROUP BY m.ts_code HAVING COUNT(DISTINCT m.l3_code) > 1")]
    if dupCodes or dupDiffL3 or ambiguous or sum(missingMap.values()) > 50:
        status = 'WARN'
    else:
        status = 'OK'
    return dict(codesWithDupCurrentRows=dupCodes, codesInMultipleL3=dupDiffL3, latestInDateAmbiguousL3=ambiguous,
                listedWithoutCurrentSw=missingMap,
                note='旧归属未关闭（out_date 空）且同 l3 有重复行；页面 ROW_NUMBER 取最新 in_date，仅并列项归属不确定',
                status=status)


def checkOptBasic(db: Db, ctx: dict) -> dict:
    rows = db.rows("SELECT opt_code, COUNT(*) FROM `stock_tushare`.`opt_basic` WHERE delist_date >= %s "
                   "AND opt_code IN ('OP510050.SH','OP510300.SH','OP510500.SH','OP588000.SH','OP159915.SZ') "
                   "GROUP BY opt_code", (ctx['today'],))
    live = {c: int(n) for c, n in rows}
    want = ['OP510050.SH', 'OP510300.SH', 'OP510500.SH', 'OP588000.SH', 'OP159915.SZ']
    lack = [c for c in want if not live.get(c)]
    return dict(liveContracts=live, missingUnderlyings=lack, status='FAIL' if lack else 'OK')


def checkIndexWeight(db: Db, ctx: dict) -> dict:
    rows = db.rows('SELECT index_code, MAX(trade_date) FROM `stock_wind`.`index_weight` GROUP BY index_code')
    latest = {c: str(toDate(d)) for c, d in rows}
    old = {c: d for c, d in latest.items() if (ctx['today'] - dt.date.fromisoformat(d)).days > 45}
    return dict(latestByIndex=latest, olderThan45d=old, status='WARN' if old else 'OK')


def checkStockCompany(db: Db, ctx: dict) -> dict:
    rows = db.rows(
        "SELECT RIGHT(b.ts_code,2), COUNT(*), MIN(b.list_date) FROM `stock_tushare`.`stock_basic` b "
        f"WHERE b.list_status='L' AND NOT {STALE_BJ_SQL.format(a='b')} AND NOT EXISTS "
        "(SELECT 1 FROM `stock_tushare`.`stock_company` c WHERE c.ts_code=b.ts_code) GROUP BY 1")
    missing = {m: int(n) for m, n, _ in rows}
    earliest = min((toDate(d) for _, _, d in rows if d), default=None)
    upd = db.one('SELECT MAX(updated_at) FROM `stock_tushare`.`stock_company`')
    return dict(listedWithoutCompany=missing, earliestMissingListDate=str(earliest), lastUpdatedAt=str(upd),
                status='WARN' if sum(missing.values()) > 20 else 'OK')


CUSTOM_CHECKS: list[tuple[str, str, Callable[[Db, dict], dict], str]] = [
    ('stock_tushare', 'trade_cal', checkTradeCal, '全部页面（交易日）'),
    ('stock_tushare', 'stock_basic', checkStockBasic, '全部页面（股票名单）'),
    ('stock_tushare', 'index_member_all_sw', checkSwMembers, '申万/fina行业'),
    ('stock_tushare', 'opt_basic', checkOptBasic, '期权T型/IV'),
    ('stock_wind', 'index_weight', checkIndexWeight, '申万/fina指数成分'),
    ('stock_tushare', 'stock_company', checkStockCompany, 'fina公司信息'),
]

KIND_FUNCS: dict[str, Callable[[Db, dict, dict, int], dict]] = {
    'daily': checkDaily, 'minute': checkDaily, 'today': checkToday,
    'snapshot': checkSnapshot, 'period': checkPeriod,
}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def runOne(name: str, func: Callable[[], dict], meta: dict) -> dict:
    t0 = time.perf_counter()
    try:
        res = func()
    except Exception as exc:  # 单表失败不拖累其它表
        logger.exception('[%s] 体检出错', name)
        res = dict(status='ERROR', error=f'{type(exc).__name__}: {exc}'[:300])
    res = dict(meta, **res, elapsedSec=round(time.perf_counter() - t0, 3))
    brief = briefOf(res)
    logger.info('[%-5s] %-34s %.2fs %s', res['status'], name, res['elapsedSec'], brief)
    if res['status'] not in ('OK', 'INFO'):
        res['brief'] = brief
    return res


def briefOf(res: dict) -> str:
    if res.get('kind') == 'custom':
        skip = {'schema', 'table', 'kind', 'page', 'status', 'elapsedSec', 'note', 'brief'}
        return json.dumps({k: v for k, v in res.items() if k not in skip}, ensure_ascii=False, default=str)[:400]
    parts = []
    for k in ('latest', 'expected', 'lagTradeDays', 'expectedPeriod', 'codes', 'codesSamePeriodLastYear', 'ratio',
              'missingVsIncome', 'missingRatio', 'latestAnnDate', 'staleRows', 'error'):
        if k in res:
            parts.append(f'{k}={res[k]}')
    bad = [f'{g}:{v["latest"]}/{v["median"]}' for g, v in (res.get('rowsVsMedian') or {}).items()
           if v['status'] != 'OK']
    if bad:
        parts.append('行数偏少=' + ','.join(bad))
    for key in ('coverage', 'liveContractCoverage'):
        badCov = [f'{g}:{v["ratio"]}' for g, v in (res.get(key) or {}).items() if v['status'] != 'OK']
        if badCov:
            parts.append(f'{key}偏低=' + ','.join(badCov))
    badNull = [f'{c}:{v["nullRows"]}' for c, v in (res.get('nulls') or {}).items()
               if isinstance(v, dict) and v['status'] != 'OK']
    if badNull:
        parts.append('空值=' + ','.join(badNull))
    return ' '.join(parts)


MARKET_CN = {'SH': '沪市', 'SZ': '深市', 'BJ': '北交所'}


def marketText(byMarket: dict) -> str:
    return '、'.join(f'{MARKET_CN.get(k, k)}{v}' for k, v in byMarket.items())


def plainReason(res: dict) -> str:
    """把一条 WARN/FAIL/ERROR 结果翻译成一句普通人能看懂的话（控制台/bat 摘要用）。"""
    if res['status'] == 'ERROR':
        return f"体检这张表时出错：{res.get('error', '未知错误')}"
    table, parts = res['table'], []
    if table == 'stock_basic' and res.get('staleBjOldCodesStillListed'):
        parts.append(f"有 {res['staleBjOldCodesStillListed']} 个北交所换代码前的旧代码仍标为在市（源头遗留，不影响页面）")
    if table == 'index_member_all_sw':
        amb = res.get('latestInDateAmbiguousL3') or []
        if amb:
            parts.append(f"{len(amb)} 只股票同一天挂了多个申万三级行业（{'、'.join(amb)}），行业归属不确定")
        if res.get('listedWithoutCurrentSw'):
            parts.append(f"{marketText(res['listedWithoutCurrentSw'])} 只股票没有申万行业")
    if table == 'stock_company' and res.get('listedWithoutCompany'):
        miss = res['listedWithoutCompany']
        parts.append(f"{sum(miss.values())} 只在市股票缺公司信息（{marketText(miss)}），表最后更新 {res.get('lastUpdatedAt')}")
    if res.get('lagTradeDays'):
        parts.append(f"最新数据停在 {res.get('latest')}，比应有的 {res.get('expected')} 晚 {res['lagTradeDays']} 个交易日")
    for key in ('coverage', 'liveContractCoverage'):
        for g, v in (res.get(key) or {}).items():
            if v.get('status') != 'OK':
                parts.append(f"{MARKET_CN.get(g, g)}覆盖率只有 {v['ratio']:.1%}")
    badRows = [f"{MARKET_CN.get(g, g)} {v['latest']}/{v['median']}" for g, v in (res.get('rowsVsMedian') or {}).items()
               if v.get('status') != 'OK']
    if badRows:
        parts.append('最新一天行数明显少于近期中位数（' + '，'.join(badRows) + '）')
    if isinstance(res.get('missingVsIncome'), dict) and res.get('missingRatio') and res['status'] != 'OK':
        parts.append(f"比利润表名单少 {sum(res['missingVsIncome'].values())} 只（缺失率 {res['missingRatio']:.2%}）")
    badNull = [c for c, v in (res.get('nulls') or {}).items() if isinstance(v, dict) and v.get('status') != 'OK']
    if badNull:
        parts.append('关键列有空值：' + '、'.join(badNull))
    return '；'.join(parts) or (res.get('brief') or briefOf(res))[:160]


def printConsoleSummary(report: dict, histPath: str | None) -> None:
    """面向双击 bat 的人：总体结论 + 每个需关注项一句话 + 结果文件位置。"""
    c = report['counts']
    print()
    print('=' * 60)
    print(f"数据体检结论：{report['overall']}    （{report['generatedAt']}，{report['tables']} 张表，耗时 {report['elapsedSec']} 秒）")
    print(f"  正常 OK={c['OK']}   提醒 WARN={c['WARN']}   失败 FAIL={c['FAIL']}   出错 ERROR={c['ERROR']}")
    attention = [r for r in report['results'] if r['status'] in ('WARN', 'FAIL', 'ERROR')]
    if attention:
        print('需要关注：')
    for r in attention:
        print(f"  [{r['status']}] {r['table']}（{r['page']}）：{plainReason(r)}")
    if histPath:
        print(f'结果文件：{LATEST_JSON}')
        print(f'历史存档：{histPath}')
        print('「市场状态」页会自动显示这次结论（刷新页面即可）。')
    print('=' * 60)


def runAll(only: list[str] | None, nDays: int) -> dict:
    t0 = time.perf_counter()
    now = dt.datetime.now()
    conn = getConn()
    try:
        db = Db(conn)
        ctx = buildContext(db, now)
        logger.info('体检开始：今天 %s 开市=%s 期望日线最新日=%s 期望盘中日=%s 期望报告期=%s',
                    ctx['today'], ctx['isOpenToday'], ctx['expectedDaily'], ctx['expectedIntraday'],
                    expectedPeriod(ctx['today']))
        results = []
        for chk in CHECKS:
            if only and chk['table'] not in only:
                continue
            meta = dict(schema=chk['schema'], table=chk['table'], kind=chk['kind'], page=chk.get('page', ''))
            results.append(runOne(f"{chk['schema']}.{chk['table']}", lambda c=chk: KIND_FUNCS[c['kind']](db, c, ctx, nDays), meta))
        for schema, table, func, page in CUSTOM_CHECKS:
            if only and table not in only:
                continue
            meta = dict(schema=schema, table=table, kind='custom', page=page)
            results.append(runOne(f'{schema}.{table}', lambda f=func: f(db, ctx), meta))
        queries = db.queryCount
    finally:
        conn.close()
    counts = {s: sum(1 for r in results if r['status'] == s) for s in ('OK', 'WARN', 'FAIL', 'ERROR')}
    overall = worst(*(r['status'] for r in results)) if results else 'OK'
    return dict(generatedAt=now.strftime('%Y-%m-%d %H:%M:%S'), today=str(ctx['today']),
                isOpenToday=ctx['isOpenToday'], expectedDaily=str(ctx['expectedDaily']),
                expectedIntraday=str(ctx['expectedIntraday']), expectedPeriod=str(expectedPeriod(ctx['today'])),
                overall=overall, counts=counts, tables=len(results), queries=queries,
                elapsedSec=round(time.perf_counter() - t0, 2), results=results)


def writeJson(report: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = report['generatedAt'].replace('-', '').replace(':', '').replace(' ', '_')
    histPath = os.path.join(OUTPUT_DIR, f'health_{stamp}.json')
    body = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    with open(histPath, 'w', encoding='utf-8') as fh:
        fh.write(body)
    tmp = LATEST_JSON + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(body)
    os.replace(tmp, LATEST_JSON)
    hist = sorted(f for f in os.listdir(OUTPUT_DIR) if f.startswith('health_') and f.endswith('.json'))
    for f in hist[:-HISTORY_KEEP]:
        os.remove(os.path.join(OUTPUT_DIR, f))
    return histPath


def main() -> int:
    ap = argparse.ArgumentParser(description='rt_monitor 依赖表数据新鲜度/覆盖面体检（只读）')
    ap.add_argument('--only', nargs='*', default=None, help='只查这些表名')
    ap.add_argument('--days', type=int, default=10, help='行数对比窗口（交易日）')
    ap.add_argument('--no-write', action='store_true', help='不写 JSON（调试）')
    args = ap.parse_args()
    setupLogger()
    try:
        report = runAll(args.only, args.days)
    except KeyboardInterrupt:
        logger.warning('用户中断体检')
        return 130
    logger.info('体检结束：总体=%s 计数=%s 表数=%d 查询数=%d 耗时=%.2fs',
                report['overall'], report['counts'], report['tables'], report['queries'], report['elapsedSec'])
    for r in report['results']:
        if r['status'] in ('WARN', 'FAIL', 'ERROR'):
            logger.info('  需关注 [%s] %s.%s（%s）%s', r['status'], r['schema'], r['table'], r['page'], briefOf(r))
    histPath = None
    if not args.no_write:
        histPath = writeJson(report)
        logger.info('结果已写入 %s 及 %s', LATEST_JSON, histPath)
    printConsoleSummary(report, histPath)
    # 退出码：只有 FAIL/ERROR 非 0；WARN 是提醒，不算失败
    return 1 if report['overall'] in ('FAIL', 'ERROR') else 0


if __name__ == '__main__':
    sys.exit(main())

# 代码更新时间：2026-10-04 21:21 新建：rt_monitor 依赖表数据新鲜度/覆盖面只读体检（基准=在市且期末前上市；申万并列归属单列）
# 代码更新时间：2026-10-04 22:37 数据体检.bat 用易懂摘要；退出码仅 FAIL/ERROR 非 0
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
