"""成交额历史极值 / 区间均值只读查询（marketinfo、index_daily；P5.1 自 monolith 迁出）。"""
from __future__ import annotations

from logger_config import get_logger
from rt_monitor.core.numbers import toFloatOrNone

logger = get_logger(__name__)

_to_float_or_none = toFloatOrNone


def _marketinfo_max_amount_last_n_trade_dates(conn, n: int = 60):
    """
    从 stock_tushare.marketinfo 取最近 n 个交易日（trade_date 倒序 LIMIT n）内
    amount（亿，与 compute_marketinfo 入库口径一致）最大的一行；并列取较新 trade_date。
    返回整数亿（与 index_daily 分支一致）、日期字符串 YYYY-MM-DD。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, amount
                FROM stock_tushare.marketinfo
                WHERE amount IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                (int(n),),
            )
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("marketinfo MAX T-60 读取失败: %s", e)
        return None, None
    if not rows:
        return None, None
    pairs = []
    for trade_date, amount in rows:
        av = _to_float_or_none(amount)
        if av is None:
            continue
        pairs.append((trade_date, av))
    if not pairs:
        return None, None
    max_amt = max(p[1] for p in pairs)
    best_dates = [p[0] for p in pairs if p[1] == max_amt]
    best_date = max(best_dates) if best_dates else None
    if best_date is None:
        return None, None
    yi = int(round(float(max_amt)))
    ds = str(best_date)[:10] if hasattr(best_date, 'year') else str(best_date)[:10]
    return yi, ds


def _marketinfo_min_amount_last_n_trade_dates(conn, n: int = 60):
    """
    从 stock_tushare.marketinfo 取最近 n 个交易日内 amount（亿）最小的一行；并列取较新 trade_date。
    返回整数亿、日期字符串 YYYY-MM-DD。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, amount
                FROM stock_tushare.marketinfo
                WHERE amount IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                (int(n),),
            )
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("marketinfo MIN T-60 读取失败: %s", e)
        return None, None
    pairs = []
    for trade_date, amount in rows:
        av = _to_float_or_none(amount)
        if av is not None:
            pairs.append((trade_date, av))
    if not pairs:
        return None, None
    min_amt = min(p[1] for p in pairs)
    best_dates = [p[0] for p in pairs if p[1] == min_amt]
    best_date = max(best_dates) if best_dates else None
    if best_date is None:
        return None, None
    yi = int(round(float(min_amt)))
    ds = str(best_date)[:10] if hasattr(best_date, 'year') else str(best_date)[:10]
    return yi, ds


def _index_daily_max_amount_last_n_trade_dates(conn, ts_code: str, n: int = 60):
    """
    从 stock_tushare.index_daily 取该 ts_code 最近 n 个交易日（trade_date 倒序 LIMIT n）内
    amount（千元）最大的记录；并列时取较新的 trade_date。
    返回整数亿（与 index_stat 成交额列展示一致）、日期字符串 YYYY-MM-DD。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, amount
                FROM stock_tushare.index_daily
                WHERE ts_code = %s AND amount IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                (ts_code, int(n)),
            )
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("index_daily MAX T-60 读取失败 ts_code=%s: %s", ts_code, e)
        return None, None
    if not rows:
        return None, None
    pairs = []
    for trade_date, amount in rows:
        av = _to_float_or_none(amount)
        if av is None:
            continue
        pairs.append((trade_date, av))
    if not pairs:
        return None, None
    max_amt = max(p[1] for p in pairs)
    best_dates = [p[0] for p in pairs if p[1] == max_amt]
    best_date = max(best_dates) if best_dates else None
    if best_date is None:
        return None, None
    yi = int(round(max_amt / 1e5))
    ds = str(best_date)[:10] if hasattr(best_date, 'year') else str(best_date)[:10]
    return yi, ds


def _index_daily_min_amount_last_n_trade_dates(conn, ts_code: str, n: int = 60):
    """
    从 stock_tushare.index_daily 取该 ts_code 最近 n 个交易日内 amount（千元）最小的一行。
    返回整数亿、日期字符串 YYYY-MM-DD。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, amount
                FROM stock_tushare.index_daily
                WHERE ts_code = %s AND amount IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                (ts_code, int(n)),
            )
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("index_daily MIN T-60 读取失败 ts_code=%s: %s", ts_code, e)
        return None, None
    pairs = []
    for trade_date, amount in rows:
        av = _to_float_or_none(amount)
        if av is not None:
            pairs.append((trade_date, av))
    if not pairs:
        return None, None
    min_amt = min(p[1] for p in pairs)
    best_dates = [p[0] for p in pairs if p[1] == min_amt]
    best_date = max(best_dates) if best_dates else None
    if best_date is None:
        return None, None
    yi = int(round(min_amt / 1e5))
    ds = str(best_date)[:10] if hasattr(best_date, 'year') else str(best_date)[:10]
    return yi, ds


def _batch_index_daily_peak_amount_last_n_trade_dates(
    conn, ts_codes: list[str], n: int = 60
) -> dict[str, tuple[int | None, str | None]]:
    """多指数一次查：各 ts_code 在最近 n 个交易日内 index_daily.amount（千元）最大的一日；并列取较新 trade_date。
    返回 {ts_code: (亿元整数, 'YYYY-MM-DD')}；失败返回 {}，由调用方逐行回退。
    依赖 MySQL 8+ 窗口函数（与仓库 MySQL 8.4 一致）。"""
    uniq = sorted({c for c in ts_codes if c})
    if not uniq:
        return {}
    placeholders = ",".join(["%s"] * len(uniq))
    sql = (
        "WITH ranked AS ("
        " SELECT ts_code, trade_date, amount,"
        " ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn"
        " FROM stock_tushare.index_daily"
        f" WHERE ts_code IN ({placeholders}) AND amount IS NOT NULL"
        ") SELECT ts_code, trade_date, amount FROM ranked WHERE rn <= %s"
    )
    params = list(uniq) + [int(n)]
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning("batch index_daily MAX T-60 失败（将回退逐行查询）: %s", e)
        return {}
    by_ts: dict[str, list[tuple]] = {}
    for row in raw:
        if not row or len(row) < 3:
            continue
        ts_code, trade_date, amount = row[0], row[1], row[2]
        by_ts.setdefault(ts_code, []).append((trade_date, amount))
    out: dict[str, tuple[int | None, str | None]] = {}
    for ts_code in uniq:
        rows = by_ts.get(ts_code) or []
        pairs = []
        for trade_date, amount in rows:
            av = _to_float_or_none(amount)
            if av is not None:
                pairs.append((trade_date, av))
        if not pairs:
            out[ts_code] = (None, None)
            continue
        max_amt = max(p[1] for p in pairs)
        best_dates = [p[0] for p in pairs if p[1] == max_amt]
        best_date = max(best_dates) if best_dates else None
        if best_date is None:
            out[ts_code] = (None, None)
        else:
            yi = int(round(max_amt / 1e5))
            ds = str(best_date)[:10] if hasattr(best_date, "year") else str(best_date)[:10]
            out[ts_code] = (yi, ds)
    return out


def _batch_index_daily_valley_amount_last_n_trade_dates(
    conn, ts_codes: list[str], n: int = 60
) -> dict[str, tuple[int | None, str | None]]:
    """多指数一次查：各 ts_code 最近 n 个交易日内 index_daily.amount（千元）最小的一日；并列取较新 trade_date。"""
    uniq = sorted({c for c in ts_codes if c})
    if not uniq:
        return {}
    placeholders = ",".join(["%s"] * len(uniq))
    sql = (
        "WITH ranked AS ("
        " SELECT ts_code, trade_date, amount,"
        " ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn"
        " FROM stock_tushare.index_daily"
        f" WHERE ts_code IN ({placeholders}) AND amount IS NOT NULL"
        ") SELECT ts_code, trade_date, amount FROM ranked WHERE rn <= %s"
    )
    params = list(uniq) + [int(n)]
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning("batch index_daily MIN T-60 失败（将回退逐行查询）: %s", e)
        return {}
    by_ts: dict[str, list[tuple]] = {}
    for row in raw:
        if not row or len(row) < 3:
            continue
        ts_code, trade_date, amount = row[0], row[1], row[2]
        by_ts.setdefault(ts_code, []).append((trade_date, amount))
    out: dict[str, tuple[int | None, str | None]] = {}
    for ts_code in uniq:
        rows = by_ts.get(ts_code) or []
        pairs = []
        for trade_date, amount in rows:
            av = _to_float_or_none(amount)
            if av is not None:
                pairs.append((trade_date, av))
        if not pairs:
            out[ts_code] = (None, None)
            continue
        min_amt = min(p[1] for p in pairs)
        best_dates = [p[0] for p in pairs if p[1] == min_amt]
        best_date = max(best_dates) if best_dates else None
        if best_date is None:
            out[ts_code] = (None, None)
        else:
            yi = int(round(min_amt / 1e5))
            ds = str(best_date)[:10] if hasattr(best_date, "year") else str(best_date)[:10]
            out[ts_code] = (yi, ds)
    return out


def _amount_extreme_to_yi_date(trade_date, amount, divisor: float) -> tuple[int | None, str | None]:
    """原始成交额转为整数亿，日期截成 YYYY-MM-DD。
    divisor=1e5：index_daily.amount 为千元；divisor=1：marketinfo.amount 已是亿。
    舍入与 MAX T-60 相同，使用 int(round(...))。
    """
    av = _to_float_or_none(amount)
    if av is None or trade_date is None:
        return None, None
    yi = int(round(float(av) / float(divisor)))
    return yi, str(trade_date)[:10]


def _marketinfo_max_amount_all(conn):
    """marketinfo 全历史 amount（亿）最大的一行；并列取较新 trade_date。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, amount
                FROM stock_tushare.marketinfo
                WHERE amount IS NOT NULL
                ORDER BY amount DESC, trade_date DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    except Exception as e:
        logger.warning("marketinfo 全历史 MAX 读取失败: %s", e)
        return None, None
    if not row:
        return None, None
    return _amount_extreme_to_yi_date(row[0], row[1], 1.0)


def _index_daily_max_amount_all(conn, ts_code: str):
    """index_daily 该 ts_code 全历史 amount（千元）最大的一行；并列取较新 trade_date。返回整数亿与日期。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, amount
                FROM stock_tushare.index_daily
                WHERE ts_code = %s AND amount IS NOT NULL
                ORDER BY amount DESC, trade_date DESC
                LIMIT 1
                """,
                (ts_code,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.warning("index_daily 全历史 MAX 读取失败 ts_code=%s: %s", ts_code, e)
        return None, None
    if not row:
        return None, None
    return _amount_extreme_to_yi_date(row[0], row[1], 1e5)


def _batch_index_daily_max_amount_all(
    conn, ts_codes: list[str]
) -> dict[str, tuple[int | None, str | None]]:
    """多指数一次查全历史 amount 最大日；并列取较新 trade_date。失败返回 {}，由调用方逐码回退。"""
    uniq = sorted({c for c in ts_codes if c})
    if not uniq:
        return {}
    placeholders = ",".join(["%s"] * len(uniq))
    sql = (
        "SELECT ts_code, trade_date, amount FROM ("
        " SELECT ts_code, trade_date, amount,"
        " ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY amount DESC, trade_date DESC) AS rn"
        " FROM stock_tushare.index_daily"
        f" WHERE ts_code IN ({placeholders}) AND amount IS NOT NULL"
        ") ranked WHERE rn = 1"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, uniq)
            raw = cur.fetchall() or []
    except Exception as e:
        logger.warning("batch index_daily 全历史 MAX 失败（将回退逐行查询）: %s", e)
        return {}
    out: dict[str, tuple[int | None, str | None]] = {ts: (None, None) for ts in uniq}
    for row in raw:
        if not row or len(row) < 3:
            continue
        out[row[0]] = _amount_extreme_to_yi_date(row[1], row[2], 1e5)
    return out


def _blank_amount_extreme_fields(row: dict) -> None:
    """成交额表峰值/谷值附加列置空，供前端用上一帧补齐。"""
    for key in (
        'idx_daily_max_all',
        'idx_daily_max_all_date',
        'idx_daily_max_t60',
        'idx_daily_max_t60_date',
        'idx_daily_min_t60',
        'idx_daily_min_t60_date',
    ):
        row[key] = None


def _index_daily_mean_amount_last_n_trade_dates(conn, ts_code: str, n: int = 20):
    """
    从 stock_tushare.index_daily 取该 ts_code 最近 n 个交易日（trade_date 倒序 LIMIT n）的
    amount（千元）算术均值，转为亿元（与 MAX T-60 同单位换算：amount/1e5）。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT amount
                FROM stock_tushare.index_daily
                WHERE ts_code = %s AND amount IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT %s
                """,
                (ts_code, int(n)),
            )
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("index_daily Ma(n) 均值读取失败 ts_code=%s n=%s: %s", ts_code, n, e)
        return None
    vals = []
    for row in rows:
        amount = row[0] if row else None
        av = _to_float_or_none(amount)
        if av is not None:
            vals.append(av)
    if not vals:
        return None
    mean_yi = (sum(vals) / len(vals)) / 1e5
    return round(mean_yi, 2)

# 代码更新时间：2026-10-04 21:54 新建：P5.1 自 monolith 迁出
