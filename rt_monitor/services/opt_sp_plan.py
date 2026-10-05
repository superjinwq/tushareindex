"""Sell Put 操作计划：收盘确认 → 次日开盘指令。

已接标的见 SP_PROFILES。账本独立叠加、无替换；一律次月钟。

588000 锁定 5/5/3/3（计划合计 160%）：
通道 50% 当月+次月、α=0.6、L6、危险仍开不强平；
Gap 50% 当月+次月、L4、危险禁开=强平；
动量 30% 只当季、L5、危险仍开不强平、须过 SMA120；
BIAS 30% 只次月、MA8、L5、危险禁开=强平、过热状态平。

510500 锁定 3/6/5/2（计划合计 160%）：
通道 30% 次月+当季、α=0.7、L7、危险仍开不强平；
Gap 60% 当月+次月、L5、危险禁开=强平；
动量 50% 当月+次月、L9、危险禁开=强平、须过 SMA120；
BIAS 20% 只当季、SMA20、L8、危险禁开=强平、过热状态平。

159915 锁定 5/5/3/3（计划合计 160%）：
通道 50% 只次月、N=25、α=0.8、L10、危险仍开不强平；
Gap 50% 只当季、p97、L4、危险禁开=强平（幽灵次月钟）；
动量 30% 只当月、L4、危险仍开不强平、须过 SMA120（幽灵次月钟）；
BIAS 30% 只次月、SMA8、L7、危险禁开=强平、过热状态平。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pymysql

from logger_config import get_logger
from rt_monitor.core.config import DB_CONFIG_TUSHARE

logger = get_logger(__name__)

CAPITAL = 1_000_000.0
CAPITAL_WAN_DEFAULT = 100
CAPITAL_WAN_MIN = 10
CAPITAL_WAN_MAX = 10000
MULT = 10000
MIN_HIST = 120
P_HI, P_HI95, P_LO, P_LO5, P_NDN = 90, 95, 10, 5, 70
BOOKS = ('通道', 'Gap', '动量', 'BIAS')
PREVIEW_BOOKS = ('通道', 'Gap')
NM_CLOCK_BOOKS = ('通道', 'Gap', '动量', 'BIAS')
PX_NEAR_ATOL = 0.00051
TENOR_KEYS = ('CM', 'NM', 'CQ', 'NQ')
TENOR_CN = {'CM': '当月', 'NM': '次月', 'CQ': '当季', 'NQ': '下季'}
TENOR_KEY_OF = {v: k for k, v in TENOR_CN.items()}
TENOR_IDX = {'CM': 0, 'NM': 1, 'CQ': 2, 'NQ': 3}
LOW_NS = (4, 5, 6, 7, 8, 9, 10)
# 春节/国庆等满一周休市后首日禁开；周末=3、五一≈5–6 不挡
LONG_HOLIDAY_REOPEN_MIN_CAL = 7

# 锁定回测：近一年窗 2025-08-11 ~ 2026-08-10；单槽=各打满 100 万（不能相加）
BT_WINDOW_1Y = '2025-08-11~2026-08-10'


def _bookW(ch: float, gap: float, mom: float, bias: float) -> dict[str, float]:
    return {'通道': ch, 'Gap': gap, '动量': mom, 'BIAS': bias, '卫星': 0.20}


def _btCombo(
    pnlWan: float,
    ddWan: float,
    calmar: float | None,
    rounds: int,
    *,
    avgNomPct: float,
    holdDayPct: float,
    peakNomPct: float,
    note: str,
    allPnlWan: float | None = None,
    allDdWan: float | None = None,
    allRounds: int | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        'window': BT_WINDOW_1Y,
        'pnlWan': pnlWan,
        'ddWan': ddWan,
        'calmar': calmar,
        'rounds': rounds,
        'avgNomPct': avgNomPct,
        'holdDayPct': holdDayPct,
        'peakNomPct': peakNomPct,
        'note': note,
    }
    if allPnlWan is not None:
        out['allPnlWan'] = allPnlWan
        out['allDdWan'] = allDdWan
        out['allRounds'] = allRounds
    return out


SP_PROFILES: dict[str, dict[str, Any]] = {
    '588000': {
        'u': '588000',
        'name': '科创50',
        'etfTs': '588000.SH',
        'optCode': 'OP588000.SH',
        'ivUnder': '588000.SH',
        'alloc': '5/5/3/3',
        'bookW': _bookW(0.50, 0.50, 0.30, 0.30),
        'chAlpha': 0.6,
        'chNHigh': 20,
        'chNLow': 6,
        'gapPThr': 95,
        'gapNLow': 4,
        'momNLow': 5,
        'biasNLow': 5,
        'biasMaN': 8,
        'skipDangerBooks': frozenset({'通道', '动量'}),
        'dangerBanOpenBooks': frozenset({'Gap', 'BIAS'}),
        'ghostNmClockBooks': frozenset({'动量'}),
        'chTenorW': {'CM': 0.5, 'NM': 0.5},
        'chCmMinDte': 15,
        'chTenorHint': '当月+次月 1:1',
        'gapTenorW': {'CM': 0.5, 'NM': 0.5},
        'gapCmMinDte': 1,
        'gapTenorHint': '当月+次月 1:1',
        'momTenorW': None,
        'momTenor': 'CQ',
        'momCmMinDte': 1,
        'momTenorHint': '只当季',
        'biasTenor': 'NM',
        'biasTenorHint': '只次月',
        'structure': '5/5/3/3 无替换 · 通道/Gap 当月+次月 · 动量只当季 · BIAS只次月 · 一律次月钟',
        'comboBt1y': _btCombo(
            34.42, -0.77, 44.49, 43,
            avgNomPct=43.7, holdDayPct=65.3, peakNomPct=128.7,
            note='合槽 5/5/3/3 无替换 · 通道L6 · GapL4 · 每100万',
        ),
    },
    '510500': {
        'u': '510500',
        'name': '中证500',
        'etfTs': '510500.SH',
        'optCode': 'OP510500.SH',
        'ivUnder': '510500.SH',
        'alloc': '3/6/5/2',
        'bookW': _bookW(0.30, 0.60, 0.50, 0.20),
        'chAlpha': 0.7,
        'chNHigh': 20,
        'chNLow': 7,
        'gapPThr': 95,
        'gapNLow': 5,
        'momNLow': 9,
        'biasNLow': 8,
        'biasMaN': 20,
        'skipDangerBooks': frozenset({'通道'}),
        'dangerBanOpenBooks': frozenset({'Gap', '动量', 'BIAS'}),
        'ghostNmClockBooks': frozenset({'BIAS'}),
        'chTenorW': {'NM': 0.5, 'CQ': 0.5},
        'chCmMinDte': 1,
        'chTenorHint': '次月+当季 1:1',
        'gapTenorW': {'CM': 0.5, 'NM': 0.5},
        'gapCmMinDte': 1,
        'gapTenorHint': '当月+次月 1:1',
        'momTenorW': {'CM': 0.5, 'NM': 0.5},
        'momTenor': 'CQ',
        'momCmMinDte': 1,
        'momTenorHint': '当月+次月 1:1',
        'biasTenor': 'CQ',
        'biasTenorHint': '只当季',
        'structure': '3/6/5/2 无替换 · 通道次月+当季 · Gap/动量当月+次月 · BIAS只当季 · 一律次月钟',
        'comboBt1y': _btCombo(
            19.54, -0.72, 27.25, 37,
            avgNomPct=43.9, holdDayPct=76.4, peakNomPct=133.3,
            allPnlWan=35.84, allDdWan=-1.04, allRounds=87,
            note='合槽 3/6/5/2 无替换 · 通道L7 · GapL5 · 动量L9 · BIAS SMA20 L8 · 每100万',
        ),
    },
    '159915': {
        'u': '159915',
        'name': '创业板',
        'etfTs': '159915.SZ',
        'optCode': 'OP159915.SZ',
        'ivUnder': '159915.SZ',
        'alloc': '5/5/3/3',
        'bookW': _bookW(0.50, 0.50, 0.30, 0.30),
        'chAlpha': 0.8,
        'chNHigh': 25,
        'chNLow': 10,
        'gapPThr': 97,
        'gapNLow': 4,
        'momNLow': 4,
        'biasNLow': 7,
        'biasMaN': 8,
        'skipDangerBooks': frozenset({'通道', '动量'}),
        'dangerBanOpenBooks': frozenset({'Gap', 'BIAS'}),
        'ghostNmClockBooks': frozenset({'Gap', '动量'}),
        'chTenorW': {'NM': 1.0},
        'chCmMinDte': 1,
        'chTenorHint': '只次月',
        'gapTenorW': {'CQ': 1.0},
        'gapCmMinDte': 1,
        'gapTenorHint': '只当季',
        'momTenorW': None,
        'momTenor': 'CM',
        'momCmMinDte': 1,
        'momTenorHint': '只当月',
        'biasTenor': 'NM',
        'biasTenorHint': '只次月',
        'structure': '5/5/3/3 无替换 · 通道只次月 N25 · Gap只当季 p97 · 动量只当月 · BIAS只次月 · 一律次月钟',
        'comboBt1y': _btCombo(
            22.46, -1.82, 12.34, 40,
            avgNomPct=41.8, holdDayPct=70.2, peakNomPct=125.3,
            allPnlWan=36.94, allDdWan=-1.82, allRounds=91,
            note='合槽 5/5/3/3 无替换 · 通道N25 α0.8 L10 · Gap p97 L4 · 动量L4 · BIAS SMA8 L7 · 每100万',
        ),
    },
}

SP_ACTIVE_U = '588000'
ETF_TS = SP_PROFILES[SP_ACTIVE_U]['etfTs']
OPT_CODE = SP_PROFILES[SP_ACTIVE_U]['optCode']
IV_UNDER = SP_PROFILES[SP_ACTIVE_U]['ivUnder']
W_CH, W_GAP, W_MOM, W_BIAS = 0.50, 0.50, 0.30, 0.30
W_CAP = W_CH + W_GAP + W_MOM + W_BIAS  # 588000 1.60，兼容旧引用
BOOK_W = SP_PROFILES[SP_ACTIVE_U]['bookW']
SKIP_DANGER_BOOKS = tuple(SP_PROFILES[SP_ACTIVE_U]['skipDangerBooks'])
CH_TENOR_W = SP_PROFILES[SP_ACTIVE_U]['chTenorW']
CH_CM_MIN_DTE = SP_PROFILES[SP_ACTIVE_U]['chCmMinDte']
CH_ALPHA = SP_PROFILES[SP_ACTIVE_U]['chAlpha']
CH_N_LOW = SP_PROFILES[SP_ACTIVE_U]['chNLow']
GAP_N_LOW = SP_PROFILES[SP_ACTIVE_U]['gapNLow']
GAP_TENOR_W = SP_PROFILES[SP_ACTIVE_U]['gapTenorW']
COMBO_BT_1Y = SP_PROFILES[SP_ACTIVE_U]['comboBt1y']

_tablesReady = False
_SP_PLAN_CACHE_TTL_SEC = 60.0
_SP_FRAME_CACHE_TTL_SEC = 180.0
_SP_PREVIEW_CACHE_TTL_SEC = 4.0
_spPlanCacheLock = threading.Lock()
_spPlanCache: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_spPlanComputeLocks: dict[tuple[str, int, int], threading.Lock] = {}
_spPlanComputeLocksGuard = threading.Lock()
_spPlanRefreshing: set[tuple[str, int, int]] = set()
_spFrameCacheLock = threading.Lock()
_spFrameCache: dict[str, tuple[float, pd.DataFrame, pd.DataFrame]] = {}
_spPreviewCacheLock = threading.Lock()
_spPreviewCache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_spPreviewComputeLocks: dict[tuple[str, int], threading.Lock] = {}
_spPreviewComputeLocksGuard = threading.Lock()
_spPreviewRefreshing: set[tuple[str, int]] = set()


def tenorKeyOf(raw: str | None) -> str:
    s = str(raw or '').strip()
    if not s:
        return ''
    if s in TENOR_CN:
        return s
    head = s.split()[0]
    if head in TENOR_CN:
        return head
    return TENOR_KEY_OF.get(s, '')


def tenorCnOf(raw: str | None) -> str:
    key = tenorKeyOf(raw)
    if key:
        return TENOR_CN[key]
    return str(raw or '').strip()


def tenorLabelOf(tenorKey: str | None, tenorCn: str | None = None) -> str:
    key = tenorKeyOf(tenorKey) or tenorKeyOf(tenorCn)
    cn = tenorCnOf(tenorCn or tenorKey)
    if key and cn:
        if cn.startswith(key + ' ') or cn == key:
            return cn if cn != key else f'{key} {TENOR_CN[key]}'
        return f'{key} {cn}'
    return key or cn or ''


def invalidateSpPlanCache() -> None:
    with _spPlanCacheLock:
        _spPlanCache.clear()
        _spPlanRefreshing.clear()
    with _spFrameCacheLock:
        _spFrameCache.clear()
    with _spPreviewCacheLock:
        _spPreviewCache.clear()
        _spPreviewRefreshing.clear()


def _py(v: Any) -> Any:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(v).strftime('%Y-%m-%d')
    return v


def expandingPctile(vals: np.ndarray) -> np.ndarray:
    out = np.full(len(vals), np.nan)
    for i, cur in enumerate(vals):
        if i + 1 < MIN_HIST or not np.isfinite(cur):
            continue
        hist = vals[: i + 1]
        hist = hist[np.isfinite(hist)]
        if len(hist) < MIN_HIST:
            continue
        less = np.sum(hist < cur)
        eq = np.sum(hist == cur)
        out[i] = (less + 0.5 * eq) / len(hist) * 100.0
    return out


def _num(r: pd.Series, key: str) -> float:
    if key not in r.index or pd.isna(r[key]):
        return float('nan')
    return float(r[key])


def attachPrevCal(etf: pd.DataFrame) -> pd.DataFrame:
    out = etf.copy()
    td = pd.to_datetime(out['trade_date'])
    out['prevCalDays'] = td.diff().dt.days
    return out


def isLongHolidayReopen(r: pd.Series) -> bool:
    d = _num(r, 'prevCalDays')
    return np.isfinite(d) and d >= LONG_HOLIDAY_REOPEN_MIN_CAL


def flagsOf(r: pd.Series, gapPThr: float | None = None) -> dict[str, Any]:
    ampP, pctP, gapP, volP = _num(r, 'amp_p'), _num(r, 'pct_p'), _num(r, 'gap_p'), _num(r, 'volRatio_p')
    ivP, biasP = _num(r, 'iv_p'), _num(r, 'bias_p')
    prior5P, prior10P, nDownP = _num(r, 'prior5_p'), _num(r, 'prior10_p'), _num(r, 'n_down10_p')
    pct = _num(r, 'pct')
    gapThr = float(P_HI95 if gapPThr is None else gapPThr)
    preDump = (np.isfinite(prior5P) and prior5P <= P_LO5) or (
        np.isfinite(prior10P) and prior10P <= P_LO5
    )
    like721 = (
        np.isfinite(ampP) and np.isfinite(pctP) and ampP >= P_HI and pctP >= P_HI
        and preDump and np.isfinite(nDownP) and nDownP >= P_NDN
    )
    smash = np.isfinite(ampP) and np.isfinite(pctP) and ampP >= P_HI and pctP <= P_LO
    ivDanger = np.isfinite(ivP) and ivP >= P_HI95 and np.isfinite(pctP) and pctP <= P_LO
    overheat = np.isfinite(biasP) and biasP >= P_HI95
    volUp = np.isfinite(volP) and np.isfinite(pctP) and volP >= P_HI and pctP >= P_HI and not like721
    biasOs = np.isfinite(biasP) and biasP <= P_LO5
    gapExt = np.isfinite(gapP) and gapP >= gapThr
    trendUp = np.isfinite(ampP) and np.isfinite(pctP) and ampP >= P_HI and pctP >= P_HI and not preDump
    momOr = (
        np.isfinite(pctP) and pctP >= P_HI and not like721
        and (
            (np.isfinite(ampP) and ampP >= P_HI)
            or (np.isfinite(volP) and volP >= P_HI)
        )
    )
    upRaw = r['up120'] if 'up120' in r.index else float('nan')
    up120 = bool(upRaw) if pd.notna(upRaw) else False
    return {
        'like721': bool(like721),
        'smash': bool(smash),
        'ivDanger': bool(ivDanger),
        'overheat': bool(overheat),
        'volUp': bool(volUp),
        'biasOs': bool(biasOs),
        'gapExt': bool(gapExt),
        'trendUp': bool(trendUp),
        'momOr': bool(momOr),
        'up120': up120,
        'danger': bool(like721 or smash or ivDanger),
        'preDump': bool(preDump),
        'isGreen': bool(np.isfinite(pct) and pct > 0),
        'holidayReopen': isLongHolidayReopen(r),
        'prevCalDays': None if not np.isfinite(_num(r, 'prevCalDays')) else int(_num(r, 'prevCalDays')),
    }


def satTrigger(f: dict[str, Any]) -> str:
    tags = []
    if f.get('volUp'):
        tags.append('放量阳')
    if f.get('biasOs'):
        tags.append('BIAS')
    if f.get('trendUp'):
        tags.append('趋势')
    return '+'.join(tags)


def satExitProfile(trigger: str) -> str:
    tags = set(str(trigger).split('+')) if trigger else set()
    if tags & {'放量阳', '趋势', 'Gap'}:
        return '通道L5'
    return '基线现行'


def parseCapitalWan(raw: Any) -> int:
    try:
        n = int(round(float(raw)))
    except (TypeError, ValueError):
        n = CAPITAL_WAN_DEFAULT
    return max(CAPITAL_WAN_MIN, min(CAPITAL_WAN_MAX, n))


def capitalYuanOf(capitalWan: int) -> float:
    return float(parseCapitalWan(capitalWan)) * 10_000.0


def spCfg(code6: str) -> dict[str, Any] | None:
    return SP_PROFILES.get(str(code6 or '').strip())


def spEnabledCodes() -> list[str]:
    return list(SP_PROFILES.keys())


def spEnabledLabel() -> str:
    return ' / '.join(SP_PROFILES)


def _spDisabledPayload(u: str) -> dict[str, Any]:
    return {
        'ok': True,
        'enabled': False,
        'u': u,
        'message': f'本标的未接入 Sell Put（已接 {spEnabledLabel()}）',
    }


def wCapOf(cfg: dict[str, Any]) -> float:
    w = cfg['bookW']
    return float(w['通道'] + w['Gap'] + w['动量'] + w['BIAS'])


def chNHighOf(cfg: dict[str, Any] | None) -> int:
    if not cfg:
        return 20
    return int(cfg.get('chNHigh') or 20)


def gapPThrOf(cfg: dict[str, Any] | None) -> float:
    if not cfg:
        return float(P_HI95)
    return float(cfg.get('gapPThr') or P_HI95)


def nLowOf(cfg: dict[str, Any], book: str) -> int:
    if book == '通道':
        return int(cfg['chNLow'])
    if book == 'Gap':
        return int(cfg['gapNLow'])
    if book == '动量':
        return int(cfg['momNLow'])
    return int(cfg['biasNLow'])


def usedLowNs(cfg: dict[str, Any]) -> tuple[int, ...]:
    return tuple(sorted({
        int(cfg['chNLow']), int(cfg['gapNLow']),
        int(cfg['momNLow']), int(cfg['biasNLow']),
    }))


def _bt1y(
    pnlWan: float,
    ddWan: float,
    calmar: float | None,
    rounds: int,
    *,
    winPct: float | None = None,
    allPnlWan: float | None = None,
    allDdWan: float | None = None,
    allRounds: int | None = None,
    note: str = '单槽打满100万',
) -> dict[str, Any]:
    return {
        'window': BT_WINDOW_1Y,
        'pnlWan': pnlWan,
        'ddWan': ddWan,
        'calmar': calmar,
        'rounds': rounds,
        'winPct': winPct,
        'allPnlWan': allPnlWan,
        'allDdWan': allDdWan,
        'allRounds': allRounds,
        'note': note,
    }


def lotsForWeight(
    spot: float,
    nTenors: int,
    weight: float,
    capital: float | None = None,
) -> int:
    if spot <= 0 or nTenors <= 0 or weight <= 0:
        return 0
    cap = CAPITAL if capital is None else float(capital)
    if cap <= 0:
        return 0
    return int(np.floor(cap * weight / (nTenors * spot * MULT)))


def ensureSpPlanTables(conn: pymysql.connections.Connection) -> None:
    global _tablesReady
    if _tablesReady:
        return
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS opt_sp_signal_daily (
            underlying varchar(20) NOT NULL COMMENT '标的6位',
            asof date NOT NULL COMMENT '已收盘日',
            spot decimal(10,4) DEFAULT NULL COMMENT 'ETF收盘',
            vol_up tinyint NOT NULL DEFAULT 0 COMMENT '放量阳',
            bias_os tinyint NOT NULL DEFAULT 0 COMMENT 'BIAS超卖',
            gap_ext tinyint NOT NULL DEFAULT 0 COMMENT '跳空P95',
            trend_up tinyint NOT NULL DEFAULT 0 COMMENT '趋势',
            channel_brk tinyint NOT NULL DEFAULT 0 COMMENT '高低20突破',
            danger tinyint NOT NULL DEFAULT 0 COMMENT '危险日',
            overheat tinyint NOT NULL DEFAULT 0 COMMENT 'BIAS过热警示',
            exit_l5 tinyint NOT NULL DEFAULT 0 COMMENT '5日最低新低',
            flags_json json DEFAULT NULL COMMENT '完整flags',
            PRIMARY KEY (underlying, asof)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        COMMENT='Sell Put信号日快照'
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS opt_sp_position (
            id bigint NOT NULL AUTO_INCREMENT,
            underlying varchar(20) NOT NULL,
            book varchar(16) NOT NULL COMMENT '通道/Gap/动量/BIAS',
            signal_name varchar(64) NOT NULL,
            ts_code varchar(32) NOT NULL,
            tenor varchar(8) NOT NULL COMMENT '当月/次月/当季/下季',
            strike decimal(10,4) NOT NULL,
            lots bigint NOT NULL,
            entry_date date NOT NULL,
            signal_date date DEFAULT NULL,
            entry_prem decimal(12,6) NOT NULL,
            entry_spot decimal(10,4) DEFAULT NULL,
            exit_profile varchar(32) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'open',
            close_date date DEFAULT NULL,
            exit_prem decimal(12,6) DEFAULT NULL,
            pnl decimal(14,2) DEFAULT NULL,
            hold_days int DEFAULT NULL,
            close_reason varchar(64) DEFAULT NULL,
            PRIMARY KEY (id),
            KEY idx_open (underlying, book, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        COMMENT='Sell Put按信号持仓'
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS opt_sp_trade_log (
            id bigint NOT NULL AUTO_INCREMENT,
            underlying varchar(20) NOT NULL,
            book varchar(16) NOT NULL,
            signal_name varchar(64) NOT NULL,
            action varchar(16) NOT NULL COMMENT '开仓/平仓',
            signal_date date DEFAULT NULL,
            exec_date date NOT NULL,
            ts_code varchar(32) NOT NULL,
            tenor varchar(8) NOT NULL,
            strike decimal(10,4) NOT NULL,
            lots bigint NOT NULL,
            fill_prem decimal(12,6) NOT NULL,
            spot decimal(10,4) DEFAULT NULL,
            pnl decimal(14,2) DEFAULT NULL,
            hold_days int DEFAULT NULL,
            reason varchar(64) DEFAULT NULL,
            note varchar(200) DEFAULT NULL,
            created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            KEY idx_u_exec (underlying, exec_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        COMMENT='Sell Put确认成交流水'
        """
    )
    conn.commit()
    _tablesReady = True


def _sqlDf(conn, sql: str, params: tuple | None = None) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(list(cur.fetchall()), columns=cols)


def enrichEtfSignals(etf: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    """
    通道 N 日高、Gap expandingPctile 的 MIN_HIST=120 都是 etf_daily 交易日行数，不是自然日。
    收盘计划与盘中预演共用。N 与 Gap 分位阈值按 profile。
    """
    out = etf.copy()
    if 'representative_iv' in out.columns:
        out['iv'] = pd.to_numeric(out['representative_iv'], errors='coerce') * 100.0
    elif 'iv' not in out.columns:
        out['iv'] = np.nan
    pre, op = out['pre_close'], out['open']
    out['pct'] = (out['close'] - pre) / pre * 100.0
    out['gap'] = (op - pre) / pre * 100.0
    out['amp'] = (out['high'] - out['low']) / pre * 100.0
    out['vol20'] = out['vol'].rolling(20, min_periods=10).mean()
    out['volRatio'] = out['vol'] / out['vol20']
    for m in ('pct', 'gap', 'amp', 'volRatio'):
        out[m + '_p'] = expandingPctile(out[m].to_numpy(float))
    out['prior5'] = (out['close'].shift(1) / out['close'].shift(6) - 1) * 100.0
    out['prior10'] = (out['close'].shift(1) / out['close'].shift(11) - 1) * 100.0
    down = (out['pct_p'] <= P_LO).astype(float)
    out['n_down10'] = down.shift(1).rolling(10, min_periods=5).sum()
    out['iv_p'] = expandingPctile(out['iv'].to_numpy(float))
    close = out['close']
    biasN = int(cfg['biasMaN'])
    out[f'ma{biasN}'] = close.rolling(biasN, min_periods=biasN).mean()
    out['biasMa'] = (close / out[f'ma{biasN}'] - 1.0) * 100.0
    out['bias_p'] = expandingPctile(out['biasMa'].to_numpy(float))
    out['ma120'] = close.rolling(120, min_periods=120).mean()
    out['up120'] = close >= out['ma120']
    out['prior5_p'] = expandingPctile(out['prior5'].to_numpy(float))
    out['prior10_p'] = expandingPctile(out['prior10'].to_numpy(float))
    out['n_down10_p'] = expandingPctile(out['n_down10'].to_numpy(float))
    hi, lo = out['high'], out['low']
    nHigh = chNHighOf(cfg)
    out['chNHigh'] = nHigh
    out['maxHN'] = hi.rolling(nHigh, min_periods=nHigh).max().shift(1)
    out['maxH20'] = hi.rolling(20, min_periods=20).max().shift(1)
    for n in LOW_NS:
        out[f'minL{n}'] = lo.rolling(n, min_periods=n).min().shift(1)
        out[f'exitL{n}'] = lo < out[f'minL{n}']
    out['brkH'] = hi > out['maxHN']
    return attachPrevCal(out)


def loadMarketFrame(
    conn: pymysql.connections.Connection,
    cfg: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    etf = _sqlDf(
        conn,
        """
        SELECT trade_date, open, high, low, close, pre_close, vol, ma20, bias20ma
        FROM etf_daily WHERE ts_code=%s ORDER BY trade_date
        """,
        (cfg['etfTs'],),
    )
    iv = _sqlDf(
        conn,
        """
        SELECT trade_date, representative_iv FROM opt_iv_panel
        WHERE underlying=%s AND tenor='current_month' AND call_put='C'
        """,
        (cfg['ivUnder'],),
    )
    basic = _sqlDf(
        conn,
        """
        SELECT ts_code, name, exercise_price, maturity_date, list_date, delist_date
        FROM opt_basic WHERE opt_code=%s AND call_put='P'
        """,
        (cfg['optCode'],),
    )
    etf['trade_date'] = pd.to_datetime(etf['trade_date'])
    for c in ('open', 'high', 'low', 'close', 'pre_close', 'vol', 'ma20', 'bias20ma'):
        etf[c] = pd.to_numeric(etf[c], errors='coerce')
    iv['trade_date'] = pd.to_datetime(iv['trade_date'])
    iv['representative_iv'] = pd.to_numeric(iv['representative_iv'], errors='coerce')
    etf = etf.merge(iv, on='trade_date', how='left')
    etf = enrichEtfSignals(etf, cfg)
    basic['maturity_date'] = pd.to_datetime(basic['maturity_date'])
    basic['delist_date'] = pd.to_datetime(basic['delist_date'])
    if 'list_date' in basic.columns:
        basic['list_date'] = pd.to_datetime(basic['list_date'])
    basic['exercise_price'] = pd.to_numeric(basic['exercise_price'], errors='coerce')
    return etf, basic


def pickAtmPuts(basic: pd.DataFrame, asof, spot: float, pxMap: dict[str, float]) -> list[dict[str, Any]]:
    asof = pd.Timestamp(asof).normalize()
    live = basic[
        (basic['maturity_date'] >= asof)
        & ((basic['delist_date'].isna()) | (basic['delist_date'] >= asof))
    ]
    if 'list_date' in live.columns:
        ld = live['list_date']
        live = live[ld.isna() | (ld <= asof)]
    if live.empty or spot <= 0:
        return []
    months = sorted(live['maturity_date'].unique())
    out: list[dict[str, Any]] = []
    for tenor in TENOR_KEYS:
        idx = TENOR_IDX[tenor]
        if idx >= len(months):
            continue
        mat = pd.Timestamp(months[idx])
        if tenor == 'CM' and int((mat - asof).days) < 1:
            continue
        cand = live[live['maturity_date'] == mat]
        if cand.empty:
            continue
        row = cand.iloc[(cand['exercise_price'] - spot).abs().argsort()].iloc[0]
        ts = str(row['ts_code'])
        px = pxMap.get(ts)
        out.append({
            'tsCode': ts,
            'name': '' if pd.isna(row.get('name')) else str(row['name']),
            'K': float(row['exercise_price']),
            'mat': mat.strftime('%Y-%m-%d'),
            'dte': int((mat - asof).days),
            'px': None if px is None else float(px),
            'tenor': TENOR_CN[tenor],
            'tenorKey': tenor,
        })
    return out


def channelAlphaOf(profile: str | None) -> float | None:
    s = str(profile or '')
    if not s.startswith('通道A'):
        return None
    try:
        return float(s[3:])
    except ValueError:
        return CH_ALPHA


def attachBookLots(
    book: str,
    spot: float,
    atm: list[dict[str, Any]],
    capital: float = CAPITAL,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    cfg = cfg or SP_PROFILES[SP_ACTIVE_U]
    w = _bookWeight(book, cfg)
    if book == '通道':
        return _attachDualTenorLots(spot, w, atm, cfg['chTenorW'], int(cfg['chCmMinDte']), capital)
    if book == 'Gap':
        return _attachDualTenorLots(spot, w, atm, cfg['gapTenorW'], int(cfg['gapCmMinDte']), capital)
    if book == '动量':
        tw = cfg.get('momTenorW')
        if tw:
            return _attachDualTenorLots(
                spot, w, atm, tw, int(cfg.get('momCmMinDte') or 1), capital,
            )
        return _attachSingleTenorLots(spot, w, atm, str(cfg['momTenor']), capital)
    if book == 'BIAS':
        return _attachSingleTenorLots(spot, w, atm, str(cfg['biasTenor']), capital)
    return []


def _attachDualTenorLots(
    spot: float,
    bookWeight: float,
    atm: list[dict[str, Any]],
    tenorW: dict[str, float],
    cmMinDte: int,
    capital: float = CAPITAL,
) -> list[dict[str, Any]]:
    picked = []
    for x in atm:
        if x.get('tenorKey') not in tenorW:
            continue
        if x.get('tenorKey') == 'CM' and int(x.get('dte') or 0) < cmMinDte:
            continue
        picked.append(x)
    if not picked:
        return []
    hasCm = any(p.get('tenorKey') == 'CM' for p in picked)
    raw = []
    for p in picked:
        extra = 0.0 if hasCm else float(tenorW.get('CM', 0.0))
        add = extra if (not hasCm and p.get('tenorKey') == 'NM') else 0.0
        raw.append(float(tenorW.get(p.get('tenorKey'), 0.0)) + add)
    s = float(sum(raw))
    if s <= 0:
        return []
    out: list[dict[str, Any]] = []
    for leg, rw in zip(picked, raw):
        frac = bookWeight * rw / s
        nLots = lotsForWeight(spot, 1, frac, capital)
        if nLots <= 0:
            continue
        item = dict(leg)
        item['lots'] = nLots
        item['notional'] = round(nLots * spot * MULT, 2)
        out.append(item)
    return out


def _attachSingleTenorLots(
    spot: float,
    bookWeight: float,
    atm: list[dict[str, Any]],
    tenorKey: str,
    capital: float = CAPITAL,
) -> list[dict[str, Any]]:
    picked = [x for x in atm if x.get('tenorKey') == tenorKey]
    if not picked or spot <= 0 or bookWeight <= 0:
        return []
    out: list[dict[str, Any]] = []
    frac = bookWeight / len(picked)
    for leg in picked:
        nLots = lotsForWeight(spot, 1, frac, capital)
        if nLots <= 0:
            continue
        item = dict(leg)
        item['lots'] = nLots
        item['notional'] = round(nLots * spot * MULT, 2)
        out.append(item)
    return out


def _tenorMats(basic: pd.DataFrame, asof) -> dict[str, pd.Timestamp]:
    asof = pd.Timestamp(asof).normalize()
    live = basic[
        (basic['maturity_date'] >= asof)
        & ((basic['delist_date'].isna()) | (basic['delist_date'] >= asof))
    ]
    if 'list_date' in live.columns:
        ld = live['list_date']
        live = live[ld.isna() | (ld <= asof)]
    if live.empty:
        return {}
    months = sorted(live['maturity_date'].unique())
    out: dict[str, pd.Timestamp] = {}
    for tenor, idx in TENOR_IDX.items():
        if idx < len(months):
            out[tenor] = pd.Timestamp(months[idx]).normalize()
    return out


def _momNmClockExpired(legs: list[dict[str, Any]], asof, basic: pd.DataFrame) -> bool:
    if not legs or basic is None or basic.empty:
        return False
    days = [p.get('entryDate') for p in legs if p.get('entryDate')]
    if not days:
        return False
    openDay = min(pd.Timestamp(d).normalize() for d in days)
    nm = _tenorMats(basic, openDay).get('NM')
    if nm is None:
        return False
    return pd.Timestamp(asof).normalize() >= nm


def loadPutPxMap(conn, asof, cfg: dict[str, Any]) -> dict[str, float]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.ts_code, d.close
        FROM opt_daily d
        JOIN opt_basic b ON b.ts_code=d.ts_code
        WHERE b.opt_code=%s AND b.call_put='P' AND d.trade_date=%s
        """,
        (cfg['optCode'], pd.Timestamp(asof).strftime('%Y-%m-%d')),
    )
    out: dict[str, float] = {}
    for ts, px in cur.fetchall():
        if px is None:
            continue
        fv = float(px)
        if fv > 0:
            out[str(ts)] = fv
    return out


def loadOpenPositions(conn, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(
        """
        SELECT id, book, signal_name, ts_code, tenor, strike, lots, entry_date,
               signal_date, entry_prem, entry_spot, exit_profile
        FROM opt_sp_position
        WHERE underlying=%s AND status='open'
        ORDER BY book, tenor
        """,
        (cfg['u'],),
    )
    rows = []
    for r in cur.fetchall():
        rows.append({
            'id': int(r['id']),
            'book': r['book'],
            'signalName': r['signal_name'],
            'tsCode': r['ts_code'],
            'tenor': tenorCnOf(r['tenor']),
            'tenorKey': tenorKeyOf(r['tenor']),
            'K': float(r['strike']),
            'lots': int(r['lots']),
            'entryDate': r['entry_date'].strftime('%Y-%m-%d') if r['entry_date'] else None,
            'signalDate': r['signal_date'].strftime('%Y-%m-%d') if r['signal_date'] else None,
            'entryPrem': float(r['entry_prem']),
            'entrySpot': None if r['entry_spot'] is None else float(r['entry_spot']),
            'exitProfile': r['exit_profile'],
        })
    return rows


def bookLowExit(book: str, last: pd.Series, cfg: dict[str, Any]) -> tuple[bool, str]:
    n = nLowOf(cfg, book)
    col = f'exitL{n}'
    flag = bool(last[col]) if pd.notna(last.get(col)) else False
    return flag, f'{n}日新低'


def decideLegExit(
    pos: dict[str, Any], asof, f: dict[str, Any], isExitLow: bool, pxClose: float | None,
    lowReason: str = '5日新低',
    cfg: dict[str, Any] | None = None,
) -> str | None:
    cfg = cfg or SP_PROFILES[SP_ACTIVE_U]
    asofTs = pd.Timestamp(asof).normalize()
    entryTs = pd.Timestamp(pos['entryDate']).normalize()
    matTs = None
    if pos.get('mat'):
        matTs = pd.Timestamp(pos['mat']).normalize()
    if matTs is not None and asofTs >= matTs:
        return '到期'
    if asofTs <= entryTs:
        return None
    skipDanger = pos['book'] in cfg['skipDangerBooks']
    if f.get('danger') and not skipDanger:
        return '危险强平'
    profile = pos.get('exitProfile') or '通道L5'
    entry = float(pos['entryPrem'])
    alpha = channelAlphaOf(profile)
    if alpha is not None:
        if pxClose is not None and pxClose <= entry * alpha:
            return '权利金止盈'
        if isExitLow:
            return lowReason
        if pxClose is not None and pxClose >= entry * 2:
            return '翻倍止损'
        return None
    if profile == '基线现行':
        if isExitLow:
            return lowReason
        if pxClose is not None and pxClose >= entry * 2:
            return '翻倍止损'
        if f.get('overheat'):
            return '状态平仓'
        return None
    if isExitLow:
        return lowReason
    if pxClose is not None and pxClose >= entry * 2:
        return '翻倍止损'
    return None


def _bookWeight(book: str, cfg: dict[str, Any] | None = None) -> float:
    w = (cfg or SP_PROFILES[SP_ACTIVE_U])['bookW']
    return float(w.get(book) or 0.0)


def _canOpen(
    book: str,
    f: dict[str, Any],
    isBrk: bool,
    cfg: dict[str, Any],
    *,
    ignoreDanger: bool = False,
) -> bool:
    if f.get('holidayReopen'):
        return False
    if (not ignoreDanger) and book in cfg['dangerBanOpenBooks'] and f.get('danger'):
        return False
    if book == '通道':
        return bool(isBrk)
    if book == 'Gap':
        return bool(f.get('gapExt'))
    if book == '动量':
        return bool(f.get('momOr')) and bool(f.get('up120'))
    if book == 'BIAS':
        return bool(f.get('biasOs'))
    return False


def _openMeta(book: str, f: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    if book == '通道':
        return (
            f'高低{chNHighOf(cfg)}/{int(cfg["chNLow"])}',
            f'通道A{float(cfg["chAlpha"]):.1f}',
        )
    if book == 'Gap':
        return 'Gap', '通道L5'
    if book == '动量':
        return '动量', '通道L5'
    if book == 'BIAS':
        return 'BIAS', '基线现行'
    trig = satTrigger(f)
    return trig, satExitProfile(trig)


def _isNmLeg(leg: dict[str, Any]) -> bool:
    ten = str(leg.get('tenor') or '')
    return ten in ('次月', 'NM')


def _bookCloseLegs(
    book: str,
    legs: list[dict[str, Any]],
    reasons: list[str | None],
    *,
    nmClockExpired: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """次月钟：硬出场全平；次月到期或动量幽灵次月钟到期则平剩余；当月到期只平当月。"""
    hard = next((x for x in reasons if x and x != '到期'), None)
    if hard:
        return list(legs), hard
    if book not in NM_CLOCK_BOOKS:
        if any(reasons):
            return list(legs), next(x for x in reasons if x)
        return [], None
    if nmClockExpired or any(_isNmLeg(leg) and r == '到期' for leg, r in zip(legs, reasons)):
        return list(legs), '到期'
    onlyExp = [leg for leg, r in zip(legs, reasons) if r == '到期']
    if onlyExp:
        return onlyExp, '到期'
    return [], None


def assemblePlan(
    last: pd.Series,
    basic: pd.DataFrame,
    positions: list[dict[str, Any]],
    pxMap: dict[str, float],
    capital: float = CAPITAL,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg or SP_PROFILES[SP_ACTIVE_U]
    f = flagsOf(last, gapPThrOf(cfg))
    asof = last['trade_date']
    spot = float(last['close'])
    isBrk = bool(last['brkH']) if pd.notna(last.get('brkH')) else False
    exitFlags: dict[str, bool] = {}
    exitLowHits: list[str] = []
    for n in usedLowNs(cfg):
        col = f'exitL{n}'
        hit = bool(last[col]) if pd.notna(last.get(col)) else False
        exitFlags[col] = hit
        if hit:
            exitLowHits.append(f'{n}日新低')
    atm = pickAtmPuts(basic, asof, spot, pxMap)
    matByCode = {}
    if basic is not None and not basic.empty:
        for _, row in basic.iterrows():
            ts = str(row['ts_code'])
            matByCode[ts] = pd.Timestamp(row['maturity_date']).strftime('%Y-%m-%d')
    for p in positions:
        if not p.get('mat'):
            p['mat'] = matByCode.get(p.get('tsCode'))
    byBook: dict[str, list[dict[str, Any]]] = {b: [] for b in BOOKS}
    for p in positions:
        byBook.setdefault(p['book'], []).append(p)
    opens, closes = [], []
    occ = {b: 0.0 for b in BOOKS}
    bookOrder = list(BOOKS)
    for b in byBook:
        if b not in bookOrder:
            bookOrder.append(b)
    for book in bookOrder:
        legs = byBook.get(book) or []
        if legs:
            occ[book] = _bookWeight(book, cfg) * 100.0
            reasons = []
            isExitLow, lowReason = bookLowExit(book, last, cfg)
            for leg in legs:
                px = pxMap.get(leg['tsCode'])
                reasons.append(decideLegExit(leg, asof, f, isExitLow, px, lowReason, cfg))
            nmClock = book in cfg['ghostNmClockBooks'] and _momNmClockExpired(legs, asof, basic)
            toClose, reason = _bookCloseLegs(book, legs, reasons, nmClockExpired=nmClock)
            if reason:
                for leg in toClose:
                    closes.append({
                        **leg,
                        'action': '平仓',
                        'reason': reason,
                        'refPrem': pxMap.get(leg['tsCode']),
                        'holdDays': int((pd.Timestamp(asof) - pd.Timestamp(leg['entryDate'])).days),
                    })
            continue
        if not _canOpen(book, f, isBrk, cfg):
            continue
        sig, prof = _openMeta(book, f, cfg)
        w = _bookWeight(book, cfg)
        legs = attachBookLots(book, spot, atm, capital, cfg)
        if not legs:
            continue
        notional = float(sum(x['notional'] for x in legs))
        opens.append({
            'book': book,
            'signalName': sig,
            'action': '开仓',
            'weightPct': w * 100.0,
            'exitProfile': prof,
            'exitHint': _exitHint(book, cfg),
            'lots': '/'.join(str(x['lots']) for x in legs),
            'notional': round(notional, 2),
            'tenorLabel': ' + '.join(
                tenorLabelOf(x.get('tenorKey'), x.get('tenor')) for x in legs
            ),
            'legs': legs,
        })
        occ[book] = w * 100.0
    holds = []
    closingBooks = {c['book'] for c in closes}
    for p in positions:
        if p['book'] in closingBooks:
            continue
        px = pxMap.get(p['tsCode'])
        tp = None
        alpha = channelAlphaOf(p.get('exitProfile'))
        if alpha is not None:
            tp = round(float(p['entryPrem']) * alpha, 6)
        holds.append({
            **p,
            'refPrem': px,
            'takeProfit': tp,
            'holdDays': int((pd.Timestamp(asof) - pd.Timestamp(p['entryDate'])).days),
            'watch': _watchText(p, cfg),
        })
    out = {
        'flags': {k: _py(v) for k, v in f.items()},
        'brkH': isBrk,
        'exitL4': bool(last['exitL4']) if pd.notna(last.get('exitL4')) else False,
        'exitL5': bool(last['exitL5']) if pd.notna(last.get('exitL5')) else False,
        'exitL6': bool(last['exitL6']) if pd.notna(last.get('exitL6')) else False,
        'exitLowHits': exitLowHits,
        'holidayReopen': bool(f.get('holidayReopen')),
        'prevCalDays': f.get('prevCalDays'),
        'spot': spot,
        'atm': atm,
        'opens': opens,
        'closes': closes,
        'holds': holds,
        'occupancy': occ,
        'occupancyPct': round(sum(occ.values()), 1),
        'occupancyCapPct': round(wCapOf(cfg) * 100.0, 1),
        'replacePolicy': 'none',
    }
    out.update(exitFlags)
    return out


def _tenorClockHint(book: str, cfg: dict[str, Any]) -> str:
    if book == '通道':
        tw = cfg.get('chTenorW') or {}
        if 'CM' in tw:
            return '次月钟（当月到期只平当月）'
        if set(tw) == {'NM'}:
            return '次月钟'
        return '次月钟（次月到期平当季）'
    if book == 'Gap':
        tw = cfg.get('gapTenorW') or {}
        if 'NM' not in tw:
            return '次月钟（不持次月，幽灵次月到期平当季）'
        return '次月钟（当月到期只平当月）'
    if book == '动量':
        if cfg.get('momTenorW'):
            return '次月钟（当月到期只平当月）'
        if cfg.get('momTenor') == 'CM':
            return '次月钟（不持次月，幽灵次月到期平当月）'
        return '次月钟（不持次月，次月到期平当季）'
    if cfg.get('biasTenor') == 'NM':
        return '次月钟'
    return '次月钟（不持次月，次月到期平当季）'


def _dangerHoldHint(book: str, cfg: dict[str, Any]) -> str:
    if book in cfg['skipDangerBooks']:
        return '危险日继续拿'
    return '危险强平'


def _exitHint(book: str, cfg: dict[str, Any] | None = None) -> str:
    """开仓表「出口」给人看的平仓条件；内部 exitProfile（通道L5 等）仍落库。"""
    cfg = cfg or SP_PROFILES[SP_ACTIVE_U]
    n = nLowOf(cfg, book)
    clock = _tenorClockHint(book, cfg)
    if book == '通道':
        return f'权利金×{float(cfg["chAlpha"]):.1f} / {n}日新低 / 翻倍；{clock}'
    if book == 'Gap':
        return f'危险强平 / {n}日新低 / 翻倍；{clock}'
    if book == '动量':
        bits = []
        if book not in cfg['skipDangerBooks']:
            bits.append('危险强平')
        bits.extend([f'{n}日新低', '翻倍'])
        return ' / '.join(bits) + f'；{clock}'
    if book == 'BIAS':
        return f'危险强平 / {n}日新低 / 翻倍 / 过热状态平；{clock}'
    return f'{n}日新低 / 翻倍 / 危险强平'


def _watchText(p: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or SP_PROFILES[SP_ACTIVE_U]
    n = nLowOf(cfg, p['book'])
    clock = _tenorClockHint(p['book'], cfg)
    danger = _dangerHoldHint(p['book'], cfg)
    if p['book'] == '通道':
        tp = p.get('takeProfit')
        tpBit = f'止盈≤{tp:.4f}；' if isinstance(tp, (int, float)) and tp else ''
        return (
            f'{tpBit}权利金×{float(cfg["chAlpha"]):.1f} / {n}日新低 / 翻倍；'
            f'{clock}；{danger}'
        )
    if p['book'] == 'Gap':
        return f'{n}日新低 / 翻倍 / 危险强平；{clock}'
    if p['book'] == '动量':
        bits = [f'{n}日新低', '翻倍']
        if p['book'] not in cfg['skipDangerBooks']:
            bits.append('危险强平')
        return ' / '.join(bits) + f'；{clock}；{danger}'
    if p['book'] == 'BIAS':
        return f'{n}日新低 / 翻倍 / 过热状态平 / 危险强平；{clock}'
    if p['exitProfile'] == '基线现行':
        return f'{n}日新低 / 翻倍 / 过热状态平 / 危险强平'
    return f'{n}日新低 / 翻倍 / 危险强平'


def upsertSignal(
    conn, last: pd.Series, f: dict[str, Any], isBrk: bool, isExitL5: bool,
    cfg: dict[str, Any],
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        REPLACE INTO opt_sp_signal_daily (
            underlying, asof, spot, vol_up, bias_os, gap_ext, trend_up,
            channel_brk, danger, overheat, exit_l5, flags_json
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            cfg['u'],
            pd.Timestamp(last['trade_date']).strftime('%Y-%m-%d'),
            float(last['close']),
            int(f.get('volUp') or 0),
            int(f.get('biasOs') or 0),
            int(f.get('gapExt') or 0),
            int(f.get('trendUp') or 0),
            int(isBrk),
            int(f.get('danger') or 0),
            int(f.get('overheat') or 0),
            int(isExitL5),
            json.dumps({k: _py(v) for k, v in f.items()}, ensure_ascii=False),
        ),
    )
    conn.commit()


def recentSignals(
    etf: pd.DataFrame,
    limit: int,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    gapThr = gapPThrOf(cfg)
    for i in range(len(etf) - 1, -1, -1):
        r = etf.iloc[i]
        f = flagsOf(r, gapThr)
        isBrk = bool(r['brkH']) if pd.notna(r.get('brkH')) else False
        tags = []
        if isBrk:
            tags.append('通道')
        if f.get('gapExt'):
            tags.append('Gap')
        if f.get('momOr'):
            tags.append('动量' if f.get('up120') else '动量(未过MA120)')
        if f.get('biasOs'):
            tags.append('BIAS')
        if f.get('holidayReopen'):
            tags.append('长假后首日')
        if not tags and not f.get('danger'):
            continue
        if not tags:
            tags = ['危险日']
        rows.append({
            'asof': pd.Timestamp(r['trade_date']).strftime('%Y-%m-%d'),
            'spot': _py(r['close']),
            'signals': tags,
            'danger': bool(f.get('danger')),
            'overheat': bool(f.get('overheat')),
        })
        if len(rows) >= limit:
            break
    return rows


def attachOpenTradeMark(
    rows: list[dict[str, Any]],
    asof,
    pxMap: dict[str, float] | None,
    openKeys: set[tuple[str, str]] | None,
) -> list[dict[str, Any]]:
    """开仓流水不落已实现盈亏；未平仓行按 asof 收盘补盯市盈亏与持仓天。"""
    if not rows or asof is None:
        return rows
    asofTs = pd.Timestamp(asof).normalize()
    pxMap = pxMap or {}
    openKeys = openKeys or set()
    for row in rows:
        if row.get('action') != '开仓':
            continue
        key = (str(row.get('book') or ''), str(row.get('tsCode') or ''))
        if key not in openKeys:
            continue
        execS = row.get('execDate')
        if execS:
            hold = int((asofTs - pd.Timestamp(execS).normalize()).days)
            row['holdDays'] = max(0, hold)
        px = pxMap.get(str(row.get('tsCode') or ''))
        fill = row.get('fillPrem')
        lots = row.get('lots')
        if px is None or fill is None or not lots:
            continue
        row['pnl'] = round((float(fill) - float(px)) * MULT * int(lots), 2)
        row['pnlMark'] = True
    return rows


def recentTrades(
    conn,
    limit: int,
    cfg: dict[str, Any],
    *,
    asof=None,
    pxMap: dict[str, float] | None = None,
    openKeys: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(
        """
        SELECT book, signal_name, action, signal_date, exec_date, ts_code, tenor,
               strike, lots, fill_prem, pnl, hold_days, reason, note
        FROM opt_sp_trade_log
        WHERE underlying=%s
        ORDER BY id DESC
        LIMIT %s
        """,
        (cfg['u'], int(limit)),
    )
    out = []
    for r in cur.fetchall():
        out.append({
            'book': r['book'],
            'signalName': r['signal_name'],
            'action': r['action'],
            'signalDate': r['signal_date'].strftime('%Y-%m-%d') if r['signal_date'] else None,
            'execDate': r['exec_date'].strftime('%Y-%m-%d') if r['exec_date'] else None,
            'tsCode': r['ts_code'],
            'tenor': tenorCnOf(r['tenor']),
            'tenorKey': tenorKeyOf(r['tenor']),
            'K': float(r['strike']),
            'lots': int(r['lots']),
            'fillPrem': float(r['fill_prem']),
            'pnl': None if r['pnl'] is None else float(r['pnl']),
            'holdDays': r['hold_days'],
            'reason': r['reason'],
            'note': r['note'],
        })
    return attachOpenTradeMark(out, asof, pxMap, openKeys)


def signalRules(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = cfg or SP_PROFILES[SP_ACTIVE_U]
    if cfg['u'] == '510500':
        return _signalRules510500(cfg)
    if cfg['u'] == '159915':
        return _signalRules159915(cfg)
    return _signalRules588000()


def _signalRules588000() -> list[dict[str, Any]]:
    return [
        {
            'name': '高低20/6 通道',
            'weight': '50%',
            'weightPct': '仓位 50% 独立账',
            'tenorHint': '当月+次月 1:1',
            'open': '最高价 > 前20日最高；当月 DTE<15 则 100% 开次月',
            'ban': '长假后首日禁开（距上一交易日≥7日历日）；不禁开危险日、不强平',
            'exit': '权利金×0.6 / 6日新低 / 翻倍；次月钟（当月到期只平当月）',
            'exitLines': ['权利金×0.6', '6日新低', '翻倍', '次月钟（当月到期只平当月）'],
            'bt1y': _bt1y(27.10, -1.38, 19.71, 15, allPnlWan=40.24, allDdWan=-1.50, allRounds=28),
        },
        {
            'name': 'Gap',
            'weight': '50%',
            'weightPct': '仓位 50% 独立账',
            'tenorHint': '当月+次月 1:1',
            'open': '跳空 ≥ P95（不要求收阳）',
            'ban': '危险日禁开=强平；长假后首日禁开',
            'exit': '危险强平 → 4日新低 → 翻倍；次月钟（当月到期只平当月）',
            'exitLines': ['危险强平', '4日新低', '翻倍', '次月钟（当月到期只平当月）'],
            'bt1y': _bt1y(24.65, -1.53, 16.10, 15, allPnlWan=42.48, allDdWan=-2.13, allRounds=34),
        },
        {
            'name': '动量',
            'weight': '30%',
            'weightPct': '仓位 30% 独立账',
            'tenorHint': '只当季',
            'open': '涨幅_p≥90 且（振幅_p≥90 或 量比_p≥90），非 like721；信号日收盘≥SMA120',
            'ban': '长假后首日禁开；危险日仍可开、不强平',
            'exit': '5日新低 → 翻倍；次月钟（不持次月，次月到期平当季）',
            'exitLines': ['5日新低', '翻倍', '次月钟（不持次月，次月到期平当季）'],
            'bt1y': _bt1y(
                21.83, 0.0, None, 5, winPct=100.0,
                note='单槽打满100万 · 近一年5轮全胜样本脆',
            ),
        },
        {
            'name': 'BIAS',
            'weight': '30%',
            'weightPct': '仓位 30% 独立账',
            'tenorHint': '只次月',
            'open': 'MA8 乖离 expanding 分位 ≤5',
            'ban': '危险日禁开=强平；长假后首日禁开',
            'exit': '危险强平 → 5日新低 → 翻倍 → 过热状态平；次月钟',
            'exitLines': ['危险强平', '5日新低', '翻倍', '过热状态平', '次月钟'],
            'bt1y': _bt1y(11.30, -2.44, 4.63, 8, allPnlWan=19.77, allDdWan=-2.44, allRounds=16),
        },
    ]


def _pctW(cfg: dict[str, Any], book: str) -> str:
    return f'{int(round(_bookWeight(book, cfg) * 100))}%'


def _signalRules510500(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    chN, gapN, momN, biasN = (
        int(cfg['chNLow']), int(cfg['gapNLow']), int(cfg['momNLow']), int(cfg['biasNLow']),
    )
    alpha = float(cfg['chAlpha'])
    return [
        {
            'name': f'高低20/{chN} 通道',
            'weight': _pctW(cfg, '通道'),
            'weightPct': f'仓位 {_pctW(cfg, "通道")} 独立账',
            'tenorHint': cfg['chTenorHint'],
            'open': '最高价 > 前20日最高',
            'ban': '长假后首日禁开（距上一交易日≥7日历日）；不禁开危险日、不强平',
            'exit': f'权利金×{alpha:.1f} / {chN}日新低 / 翻倍；次月钟（次月到期平当季）',
            'exitLines': [
                f'权利金×{alpha:.1f}', f'{chN}日新低', '翻倍', '次月钟（次月到期平当季）',
            ],
            'bt1y': _bt1y(14.28, -2.39, 5.98, 13, allPnlWan=25.04, allDdWan=-4.10, allRounds=37),
        },
        {
            'name': 'Gap',
            'weight': _pctW(cfg, 'Gap'),
            'weightPct': f'仓位 {_pctW(cfg, "Gap")} 独立账',
            'tenorHint': cfg['gapTenorHint'],
            'open': '跳空 ≥ P95（不要求收阳）',
            'ban': '危险日禁开=强平；长假后首日禁开',
            'exit': f'危险强平 → {gapN}日新低 → 翻倍；次月钟（当月到期只平当月）',
            'exitLines': ['危险强平', f'{gapN}日新低', '翻倍', '次月钟（当月到期只平当月）'],
            'bt1y': _bt1y(15.91, -0.77, 20.55, 11, allPnlWan=28.34, allDdWan=-1.23, allRounds=27),
        },
        {
            'name': '动量',
            'weight': _pctW(cfg, '动量'),
            'weightPct': f'仓位 {_pctW(cfg, "动量")} 独立账',
            'tenorHint': cfg['momTenorHint'],
            'open': '涨幅_p≥90 且（振幅_p≥90 或 量比_p≥90），非 like721；信号日收盘≥SMA120',
            'ban': '危险日禁开=强平；长假后首日禁开；未过 MA120 不开',
            'exit': f'危险强平 → {momN}日新低 → 翻倍；次月钟（当月到期只平当月）',
            'exitLines': ['危险强平', f'{momN}日新低', '翻倍', '次月钟（当月到期只平当月）'],
            'bt1y': _bt1y(9.88, -0.64, 15.43, 6, allPnlWan=21.56, allDdWan=-1.13, allRounds=10),
        },
        {
            'name': 'BIAS',
            'weight': _pctW(cfg, 'BIAS'),
            'weightPct': f'仓位 {_pctW(cfg, "BIAS")} 独立账',
            'tenorHint': cfg['biasTenorHint'],
            'open': f'SMA{int(cfg["biasMaN"])} 乖离 expanding 分位 ≤5',
            'ban': '危险日禁开=强平；长假后首日禁开',
            'exit': f'危险强平 → {biasN}日新低 → 翻倍 → 过热状态平；次月钟（不持次月，次月到期平当季）',
            'exitLines': [
                '危险强平', f'{biasN}日新低', '翻倍', '过热状态平',
                '次月钟（不持次月，次月到期平当季）',
            ],
            'bt1y': _bt1y(12.11, -4.78, 2.53, 7, allPnlWan=19.41, allDdWan=-5.19, allRounds=13),
        },
    ]


def _signalRules159915(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    chN, gapN, momN, biasN = (
        int(cfg['chNLow']), int(cfg['gapNLow']), int(cfg['momNLow']), int(cfg['biasNLow']),
    )
    nHigh = chNHighOf(cfg)
    alpha = float(cfg['chAlpha'])
    pThr = int(gapPThrOf(cfg))
    chClock = _tenorClockHint('通道', cfg)
    gapClock = _tenorClockHint('Gap', cfg)
    momClock = _tenorClockHint('动量', cfg)
    biasClock = _tenorClockHint('BIAS', cfg)
    return [
        {
            'name': f'高低{nHigh}/{chN} 通道',
            'weight': _pctW(cfg, '通道'),
            'weightPct': f'仓位 {_pctW(cfg, "通道")} 独立账',
            'tenorHint': cfg['chTenorHint'],
            'open': f'最高价 > 前{nHigh}日最高',
            'ban': '长假后首日禁开（距上一交易日≥7日历日）；不禁开危险日、不强平',
            'exit': f'权利金×{alpha:.1f} / {chN}日新低 / 翻倍；{chClock}',
            'exitLines': [f'权利金×{alpha:.1f}', f'{chN}日新低', '翻倍', chClock],
            'bt1y': _bt1y(18.94, -2.68, 7.07, 17, allPnlWan=26.75, allDdWan=-2.68, allRounds=36),
        },
        {
            'name': 'Gap',
            'weight': _pctW(cfg, 'Gap'),
            'weightPct': f'仓位 {_pctW(cfg, "Gap")} 独立账',
            'tenorHint': cfg['gapTenorHint'],
            'open': f'跳空 ≥ P{pThr}（不要求收阳）',
            'ban': '危险日禁开=强平；长假后首日禁开',
            'exit': f'危险强平 → {gapN}日新低 → 翻倍；{gapClock}',
            'exitLines': ['危险强平', f'{gapN}日新低', '翻倍', gapClock],
            'bt1y': _bt1y(15.96, -1.10, 14.45, 13, allPnlWan=26.40, allDdWan=-1.81, allRounds=30),
        },
        {
            'name': '动量',
            'weight': _pctW(cfg, '动量'),
            'weightPct': f'仓位 {_pctW(cfg, "动量")} 独立账',
            'tenorHint': cfg['momTenorHint'],
            'open': '涨幅_p≥90 且（振幅_p≥90 或 量比_p≥90），非 like721；信号日收盘≥SMA120',
            'ban': '长假后首日禁开；危险日仍可开、不强平；未过 MA120 不开',
            'exit': f'{momN}日新低 → 翻倍；{momClock}',
            'exitLines': [f'{momN}日新低', '翻倍', momClock],
            'bt1y': _bt1y(
                8.29, -0.36, None, 5, winPct=80.0,
                allPnlWan=18.15, allDdWan=-2.25, allRounds=13,
                note='单槽打满100万 · 近一年5轮样本脆',
            ),
        },
        {
            'name': 'BIAS',
            'weight': _pctW(cfg, 'BIAS'),
            'weightPct': f'仓位 {_pctW(cfg, "BIAS")} 独立账',
            'tenorHint': cfg['biasTenorHint'],
            'open': f'SMA{int(cfg["biasMaN"])} 乖离 expanding 分位 ≤5',
            'ban': '危险日禁开=强平；长假后首日禁开',
            'exit': f'危险强平 → {biasN}日新低 → 翻倍 → 过热状态平；{biasClock}',
            'exitLines': ['危险强平', f'{biasN}日新低', '翻倍', '过热状态平', biasClock],
            'bt1y': _bt1y(9.97, -0.65, 15.33, 5, allPnlWan=21.41, allDdWan=-0.65, allRounds=12),
        },
    ]


def nextOpenCalDate(conn, asof) -> str:
    asofS = pd.Timestamp(asof).strftime('%Y-%m-%d')
    cur = conn.cursor()
    row = None
    try:
        cur.execute(
            """
            SELECT cal_date FROM trade_cal
            WHERE exchange=%s AND is_open=1 AND cal_date>%s
            ORDER BY cal_date ASC LIMIT 1
            """,
            ('SSE', asofS),
        )
        row = cur.fetchone()
    except Exception:
        logger.warning('trade_cal 下一交易日查询失败', exc_info=True)
    if row:
        d = row[0]
        if hasattr(d, 'isoformat'):
            return d.isoformat()[:10]
        return str(d)[:10]
    dt = pd.Timestamp(asof) + pd.Timedelta(days=1)
    while int(dt.weekday()) >= 5:
        dt += pd.Timedelta(days=1)
    return dt.strftime('%Y-%m-%d')


def _pxNear(a: Any, b: Any, atol: float = PX_NEAR_ATOL) -> bool:
    try:
        fa = float(a)
        fb = float(b)
    except (TypeError, ValueError):
        return False
    if fa != fa or fb != fb:
        return False
    return abs(fa - fb) <= atol


def _finitePx(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x or x <= 0:
        return None
    return x


def extractQmtEtfTick(packed: Any) -> dict[str, float] | None:
    if not isinstance(packed, dict):
        return None
    from rt_monitor.services.opt_t_board import isStubDaqmtEtfTick

    if isStubDaqmtEtfTick(packed):
        return None
    last = _finitePx(packed.get('last') if packed.get('last') is not None else packed.get('lastPrice'))
    openPx = _finitePx(packed.get('open'))
    high = _finitePx(packed.get('high'))
    low = _finitePx(packed.get('low'))
    lastClose = _finitePx(packed.get('lastClose'))
    if last is None or openPx is None or high is None or low is None:
        return None
    high = max(high, last)
    low = min(low, last)
    vol = packed.get('vol')
    if vol is None:
        vol = packed.get('volume')
    try:
        volF = float(vol) if vol is not None else float('nan')
    except (TypeError, ValueError):
        volF = float('nan')
    return {
        'last': last,
        'open': openPx,
        'high': high,
        'low': low,
        'lastClose': lastClose if lastClose is not None else float('nan'),
        'vol': volF,
    }


def isYesterdayFrozenBar(lastDaily: pd.Series, tick: dict[str, float]) -> bool:
    """快照仍是 etf_daily 末日那根 K：不预演。"""
    return (
        _pxNear(tick.get('lastClose'), lastDaily.get('pre_close'))
        and _pxNear(tick.get('open'), lastDaily.get('open'))
        and _pxNear(tick.get('high'), lastDaily.get('high'))
        and _pxNear(tick.get('low'), lastDaily.get('low'))
        and _pxNear(tick.get('last'), lastDaily.get('close'))
    )


def mergeLiveOptPxMap(
    dailyPxMap: dict[str, float],
    optSnap: Any,
    *,
    asof: date | None = None,
) -> tuple[dict[str, float], int]:
    from rt_monitor.services.opt_t_board import isFreshDaqmtOptTick, qmtOptToTsCode

    out = dict(dailyPxMap)
    liveN = 0
    if not isinstance(optSnap, dict):
        return out, liveN
    asofDate = asof or date.today()
    for qmtCode, packed in optSnap.items():
        if not isinstance(packed, dict):
            continue
        last = _finitePx(packed.get('last') if packed.get('last') is not None else packed.get('lastPrice'))
        if last is None:
            continue
        tsCode = qmtOptToTsCode(str(qmtCode))
        if not isFreshDaqmtOptTick(packed, asofDate, dailyClose=out.get(tsCode)):
            continue
        out[tsCode] = last
        liveN += 1
    return out, liveN


def _previewInactive(reason: str) -> dict[str, Any]:
    return {
        'active': False,
        'reason': reason,
        'opens': [],
        'closes': [],
    }


def buildSpPreview(
    etf: pd.DataFrame,
    basic: pd.DataFrame,
    positions: list[dict[str, Any]],
    tick: dict[str, float] | None,
    optSnap: Any,
    capital: float,
    cfg: dict[str, Any],
    *,
    today: date,
    dailyPxMap: dict[str, float] | None = None,
    clock: str = '',
) -> dict[str, Any]:
    """
    通道/Gap 盘中预演。昨收用 etf_daily 末日 close；N 日高与 Gap 分位按 profile。
    不写库、不算动量/BIAS、不预演危险强平。
    """
    if etf is None or etf.empty:
        return _previewInactive('no_etf')
    if tick is None:
        return _previewInactive('no_snapshot')
    lastDaily = etf.iloc[-1]
    lastTd = pd.Timestamp(lastDaily['trade_date']).normalize()
    todayTs = pd.Timestamp(today).normalize()
    if lastTd >= todayTs:
        return _previewInactive('daily_caught_up')
    if isYesterdayFrozenBar(lastDaily, tick):
        return _previewInactive('yesterday_bar')
    rawCols = ['trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'vol']
    if 'representative_iv' in etf.columns:
        rawCols.append('representative_iv')
    hist = etf[rawCols].copy()
    preClose = float(lastDaily['close'])
    todayRow = {
        'trade_date': todayTs,
        'open': tick['open'],
        'high': tick['high'],
        'low': tick['low'],
        'close': tick['last'],
        'pre_close': preClose,
        'vol': tick.get('vol', float('nan')),
    }
    if 'representative_iv' in hist.columns:
        todayRow['representative_iv'] = float('nan')
    live = enrichEtfSignals(pd.concat([hist, pd.DataFrame([todayRow])], ignore_index=True), cfg)
    last = live.iloc[-1]
    f = flagsOf(last, gapPThrOf(cfg))
    f['danger'] = False
    f['overheat'] = False
    isBrk = bool(last['brkH']) if pd.notna(last.get('brkH')) else False
    pxMap, optLiveN = mergeLiveOptPxMap(dailyPxMap or {}, optSnap, asof=today)
    spot = float(tick['last'])
    asof = todayTs
    atm = pickAtmPuts(basic, asof, spot, pxMap)
    matByCode = {}
    if basic is not None and not basic.empty:
        for _, row in basic.iterrows():
            ts = str(row['ts_code'])
            matByCode[ts] = pd.Timestamp(row['maturity_date']).strftime('%Y-%m-%d')
    for p in positions:
        if not p.get('mat'):
            p['mat'] = matByCode.get(p.get('tsCode'))
    byBook: dict[str, list[dict[str, Any]]] = {b: [] for b in PREVIEW_BOOKS}
    for p in positions:
        if p.get('book') in byBook:
            byBook[p['book']].append(p)
    opens: list[dict[str, Any]] = []
    closes: list[dict[str, Any]] = []
    for book in PREVIEW_BOOKS:
        legs = byBook.get(book) or []
        if legs:
            isExitLow, lowReason = bookLowExit(book, last, cfg)
            reasons = []
            for leg in legs:
                px = pxMap.get(leg['tsCode'])
                reasons.append(decideLegExit(leg, asof, f, isExitLow, px, lowReason, cfg))
            toClose, reason = _bookCloseLegs(book, legs, reasons, nmClockExpired=False)
            if reason:
                for leg in toClose:
                    closes.append({
                        **leg,
                        'action': '平仓',
                        'reason': reason,
                        'refPrem': pxMap.get(leg['tsCode']),
                        'holdDays': int((pd.Timestamp(asof) - pd.Timestamp(leg['entryDate'])).days),
                    })
            continue
        if not _canOpen(book, f, isBrk, cfg, ignoreDanger=True):
            continue
        sig, prof = _openMeta(book, f, cfg)
        w = _bookWeight(book, cfg)
        newLegs = attachBookLots(book, spot, atm, capital, cfg)
        if not newLegs:
            continue
        notional = float(sum(x['notional'] for x in newLegs))
        opens.append({
            'book': book,
            'signalName': sig,
            'action': '开仓',
            'weightPct': w * 100.0,
            'exitProfile': prof,
            'exitHint': _exitHint(book, cfg),
            'lots': '/'.join(str(x['lots']) for x in newLegs),
            'notional': round(notional, 2),
            'tenorLabel': ' + '.join(
                tenorLabelOf(x.get('tenorKey'), x.get('tenor')) for x in newLegs
            ),
            'legs': newLegs,
        })
    clockTxt = str(clock or '')
    liveLabel = f'实时 {clockTxt[11:19]}' if len(clockTxt) >= 19 else '实时'
    return {
        'active': True,
        'reason': '',
        'asof': todayTs.strftime('%Y-%m-%d'),
        'clock': liveLabel,
        'spot': spot,
        'preClose': preClose,
        'open': float(tick['open']),
        'high': float(tick['high']),
        'low': float(tick['low']),
        'gapPct': None if not np.isfinite(_num(last, 'gap')) else round(float(last['gap']), 4),
        'gapP': None if not np.isfinite(_num(last, 'gap_p')) else round(float(last['gap_p']), 2),
        'maxH20': None if pd.isna(last.get('maxH20')) else float(last['maxH20']),
        'maxHN': None if pd.isna(last.get('maxHN')) else float(last['maxHN']),
        'chNHigh': chNHighOf(cfg),
        'gapPThr': gapPThrOf(cfg),
        'brkH': isBrk,
        'gapExt': bool(f.get('gapExt')),
        'holidayReopen': bool(f.get('holidayReopen')),
        'dangerPending': True,
        'premSource': 'daqmt' if optLiveN > 0 else 'opt_daily',
        'optLiveN': optLiveN,
        'opens': opens,
        'closes': closes,
    }


def _loadSpFrameCached(conn, cfg: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    key = str(cfg['u'])
    now = time.monotonic()
    with _spFrameCacheLock:
        hit = _spFrameCache.get(key)
        if hit is not None and (now - hit[0]) < _SP_FRAME_CACHE_TTL_SEC:
            return hit[1].copy(), hit[2].copy()
    etf, basic = loadMarketFrame(conn, cfg)
    with _spFrameCacheLock:
        _spFrameCache[key] = (time.monotonic(), etf.copy(), basic.copy())
    return etf, basic


def _querySpPreviewUncached(
    code6: str,
    *,
    capitalWan: Any = CAPITAL_WAN_DEFAULT,
    snapshot: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """盘中预演计算。不写 opt_sp_signal_daily，不改正式 opens。"""
    u = str(code6 or '').strip()
    cfg = spCfg(u)
    if cfg is None:
        return {
            'ok': True,
            'enabled': False,
            'u': u,
            'preview': _previewInactive('not_enabled'),
        }
    from rt_monitor.services.opt_t_board import fetchDaqmtQuoteSnapshot

    snap = snapshot if snapshot is not None else fetchDaqmtQuoteSnapshot()
    asofDay = today or date.today()
    wan = parseCapitalWan(capitalWan)
    capital = capitalYuanOf(wan)
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        ensureSpPlanTables(conn)
        etf, basic = _loadSpFrameCached(conn, cfg)
        if etf.empty:
            return {
                'ok': True,
                'enabled': True,
                'u': u,
                'preview': _previewInactive('no_etf'),
            }
        lastDaily = etf.iloc[-1]
        dailyPx = loadPutPxMap(conn, lastDaily['trade_date'], cfg)
        positions = loadOpenPositions(conn, cfg)
    finally:
        conn.close()
    etfTick = None
    optSnap = {}
    clock = ''
    if isinstance(snap, dict):
        etfMap = snap.get('etf') if isinstance(snap.get('etf'), dict) else {}
        etfTick = extractQmtEtfTick(etfMap.get(cfg['etfTs']))
        optSnap = snap.get('opt') if isinstance(snap.get('opt'), dict) else {}
        clock = str(snap.get('etfWall') or snap.get('optWall') or '')
    preview = buildSpPreview(
        etf,
        basic,
        positions,
        etfTick,
        optSnap,
        capital,
        cfg,
        today=asofDay,
        dailyPxMap=dailyPx,
        clock=clock,
    )
    return {
        'ok': True,
        'enabled': True,
        'u': u,
        'name': cfg['name'],
        'capitalWan': wan,
        'preview': preview,
    }


def _spPreviewComputeLock(key: tuple[str, int]) -> threading.Lock:
    with _spPreviewComputeLocksGuard:
        lock = _spPreviewComputeLocks.get(key)
        if lock is None:
            lock = threading.Lock()
            _spPreviewComputeLocks[key] = lock
        return lock


def _refreshSpPreviewWorker(key: tuple[str, int]) -> None:
    u, wan = key
    try:
        lock = _spPreviewComputeLock(key)
        with lock:
            now = time.monotonic()
            with _spPreviewCacheLock:
                hit = _spPreviewCache.get(key)
                if hit is not None and (now - hit[0]) < _SP_PREVIEW_CACHE_TTL_SEC:
                    return
            data = _querySpPreviewUncached(u, capitalWan=wan)
            with _spPreviewCacheLock:
                _spPreviewCache[key] = (time.monotonic(), data)
    except Exception:
        logger.exception('Sell Put 预演后台刷新失败 u=%s', u)
    finally:
        with _spPreviewCacheLock:
            _spPreviewRefreshing.discard(key)


def _scheduleSpPreviewRefresh(key: tuple[str, int]) -> None:
    with _spPreviewCacheLock:
        if key in _spPreviewRefreshing:
            return
        _spPreviewRefreshing.add(key)
    threading.Thread(
        target=_refreshSpPreviewWorker,
        args=(key,),
        daemon=True,
        name=f'opt-sp-preview-swr-{key[0]}',
    ).start()


def querySpPreview(
    code6: str,
    *,
    capitalWan: Any = CAPITAL_WAN_DEFAULT,
    snapshot: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """盘中预演：8s TTL；过期秒返旧值并后台重算。显式 snapshot/today 走直算（测试）。"""
    u = str(code6 or '').strip()
    if snapshot is not None or today is not None:
        return _querySpPreviewUncached(
            u, capitalWan=capitalWan, snapshot=snapshot, today=today
        )
    if spCfg(u) is None:
        return _querySpPreviewUncached(u, capitalWan=capitalWan)
    wan = parseCapitalWan(capitalWan)
    key = (u, wan)
    now = time.monotonic()
    with _spPreviewCacheLock:
        hit = _spPreviewCache.get(key)
        if hit is not None:
            t0, payload = hit[0], hit[1]
            if (now - t0) < _SP_PREVIEW_CACHE_TTL_SEC:
                return payload
            stalePayload = payload
        else:
            stalePayload = None
    if stalePayload is not None:
        _scheduleSpPreviewRefresh(key)
        return stalePayload
    lock = _spPreviewComputeLock(key)
    with lock:
        now2 = time.monotonic()
        with _spPreviewCacheLock:
            hit = _spPreviewCache.get(key)
            if hit is not None and (now2 - hit[0]) < _SP_PREVIEW_CACHE_TTL_SEC:
                return hit[1]
        data = _querySpPreviewUncached(u, capitalWan=wan)
        with _spPreviewCacheLock:
            _spPreviewCache[key] = (time.monotonic(), data)
        return data


def _querySpPlanUncached(
    code6: str,
    *,
    historyLimit: int = 5,
    capitalWan: Any = CAPITAL_WAN_DEFAULT,
) -> dict[str, Any]:
    u = str(code6 or '').strip()
    cfg = spCfg(u)
    if cfg is None:
        return _spDisabledPayload(u)
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        ensureSpPlanTables(conn)
        etf, basic = _loadSpFrameCached(conn, cfg)
        if etf.empty:
            return {'ok': False, 'enabled': True, 'u': u, 'error': f'etf_daily 无 {cfg["etfTs"]}'}
        last = etf.iloc[-1]
        f = flagsOf(last, gapPThrOf(cfg))
        isBrk = bool(last['brkH']) if pd.notna(last.get('brkH')) else False
        isExitL5 = bool(last['exitL5']) if pd.notna(last.get('exitL5')) else False
        upsertSignal(conn, last, f, isBrk, isExitL5, cfg)
        asof = pd.Timestamp(last['trade_date'])
        pxMap = loadPutPxMap(conn, asof, cfg)
        positions = loadOpenPositions(conn, cfg)
        capitalWan = parseCapitalWan(capitalWan)
        capital = capitalYuanOf(capitalWan)
        plan = assemblePlan(last, basic, positions, pxMap, capital, cfg)
        nextTd = nextOpenCalDate(conn, asof)
        return {
            'ok': True,
            'enabled': True,
            'u': u,
            'name': cfg['name'],
            'asof': asof.strftime('%Y-%m-%d'),
            'planFor': f'下一交易日：{nextTd}',
            'nextTradeDate': nextTd,
            'capital': capital,
            'capitalWan': capitalWan,
            'comboBt1y': cfg['comboBt1y'],
            'structure': cfg['structure'],
            'rules': signalRules(cfg),
            'recentSignals': recentSignals(etf, historyLimit, cfg),
            'recentTrades': recentTrades(
                conn, 40, cfg, asof=asof, pxMap=pxMap,
                openKeys={(p['book'], p['tsCode']) for p in positions},
            ),
            **plan,
        }
    finally:
        conn.close()


def peekSpPlan(
    code6: str,
    *,
    historyLimit: int = 5,
    capitalWan: Any = CAPITAL_WAN_DEFAULT,
) -> dict[str, Any] | None:
    """只读内存缓存，未命中不计算。未接入标的直接返回 disabled 壳。"""
    u = str(code6 or '').strip()
    if spCfg(u) is None:
        return _spDisabledPayload(u)
    hist = int(historyLimit)
    wan = parseCapitalWan(capitalWan)
    key = (u, hist, wan)
    with _spPlanCacheLock:
        hit = _spPlanCache.get(key)
        if hit is None:
            return None
        return hit[1]


def querySpPlan(
    code6: str,
    *,
    historyLimit: int = 5,
    capitalWan: Any = CAPITAL_WAN_DEFAULT,
) -> dict[str, Any]:
    u = str(code6 or '').strip()
    if spCfg(u) is None:
        return _spDisabledPayload(u)
    hist = int(historyLimit)
    wan = parseCapitalWan(capitalWan)
    key = (u, hist, wan)
    now = time.monotonic()
    with _spPlanCacheLock:
        hit = _spPlanCache.get(key)
        if hit is not None:
            t0, payload = hit[0], hit[1]
            if (now - t0) < _SP_PLAN_CACHE_TTL_SEC:
                return payload
            stalePayload = payload
        else:
            stalePayload = None
    if stalePayload is not None:
        _scheduleSpPlanRefresh(key)
        return stalePayload
    lock = _spPlanComputeLock(key)
    with lock:
        now = time.monotonic()
        with _spPlanCacheLock:
            hit = _spPlanCache.get(key)
            if hit is not None and (now - hit[0]) < _SP_PLAN_CACHE_TTL_SEC:
                return hit[1]
        data = _querySpPlanUncached(u, historyLimit=hist, capitalWan=wan)
        with _spPlanCacheLock:
            _spPlanCache[key] = (time.monotonic(), data)
        return data


def _spPlanComputeLock(key: tuple[str, int, int]) -> threading.Lock:
    with _spPlanComputeLocksGuard:
        lock = _spPlanComputeLocks.get(key)
        if lock is None:
            lock = threading.Lock()
            _spPlanComputeLocks[key] = lock
        return lock


def _refreshSpPlanCacheWorker(key: tuple[str, int, int]) -> None:
    u, hist, wan = key
    try:
        lock = _spPlanComputeLock(key)
        with lock:
            now = time.monotonic()
            with _spPlanCacheLock:
                hit = _spPlanCache.get(key)
                if hit is not None and (now - hit[0]) < _SP_PLAN_CACHE_TTL_SEC:
                    return
            data = _querySpPlanUncached(u, historyLimit=hist, capitalWan=wan)
            with _spPlanCacheLock:
                _spPlanCache[key] = (time.monotonic(), data)
    except Exception:
        logger.exception('Sell Put 后台刷新失败 u=%s', u)
    finally:
        with _spPlanCacheLock:
            _spPlanRefreshing.discard(key)


def _scheduleSpPlanRefresh(key: tuple[str, int, int]) -> None:
    with _spPlanCacheLock:
        if key in _spPlanRefreshing:
            return
        _spPlanRefreshing.add(key)
    threading.Thread(
        target=_refreshSpPlanCacheWorker,
        args=(key,),
        daemon=True,
        name=f'opt-sp-plan-swr-{key[0]}',
    ).start()


def _holidayReopenOn(conn, sigDate: str, cfg: dict[str, Any]) -> bool:
    if not sigDate:
        return False
    cur = conn.cursor()
    cur.execute(
        """
        SELECT trade_date FROM etf_daily
        WHERE ts_code=%s AND trade_date<=%s
        ORDER BY trade_date DESC LIMIT 2
        """,
        (cfg['etfTs'], str(sigDate)[:10]),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return False
    a, b = pd.Timestamp(rows[1][0]), pd.Timestamp(rows[0][0])
    return int((b - a).days) >= LONG_HOLIDAY_REOPEN_MIN_CAL


def confirmSpFill(payload: dict[str, Any]) -> dict[str, Any]:
    u = str(payload.get('u') or '').strip()
    cfg = spCfg(u)
    if cfg is None:
        return {'ok': False, 'error': f'仅 {spEnabledLabel()} 可确认成交'}
    book = str(payload.get('book') or '').strip()
    action = str(payload.get('action') or '').strip()
    bookOk = book in BOOKS or (book == '卫星' and action == '平仓')
    if (not bookOk) or action not in ('开仓', '平仓'):
        return {'ok': False, 'error': 'book/action 无效'}
    fills = payload.get('fills') or []
    if not isinstance(fills, list) or not fills:
        return {'ok': False, 'error': '缺少 fills'}
    execDate = str(payload.get('execDate') or date.today().isoformat())
    note = str(payload.get('note') or '')[:200]
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        ensureSpPlanTables(conn)
        if action == '开仓':
            sigDate = str(payload.get('signalDate') or '')
            if _holidayReopenOn(conn, sigDate, cfg):
                return {'ok': False, 'error': '长假后首日禁开（春节/国庆等距上一交易日≥7日历日）'}
            _confirmOpen(conn, book, fills, execDate, payload, note, cfg)
        else:
            _confirmClose(conn, book, fills, execDate, payload, note, cfg)
        conn.commit()
        invalidateSpPlanCache()
        return _querySpPlanUncached(
            u, capitalWan=parseCapitalWan(payload.get('capitalWan')),
        )
    except Exception as e:
        conn.rollback()
        logger.exception('confirmSpFill 失败')
        return {'ok': False, 'error': str(e)}
    finally:
        conn.close()


def _confirmOpen(
    conn, book: str, fills: list, execDate: str, payload: dict, note: str,
    cfg: dict[str, Any],
) -> None:
    u = cfg['u']
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM opt_sp_position WHERE underlying=%s AND book=%s AND status='open'",
        (u, book),
    )
    if int(cur.fetchone()[0]) > 0:
        raise ValueError(f'{book} 已有持仓，不能再开')
    sig = str(payload.get('signalName') or book)
    if book == '通道':
        defaultProf = f'通道A{float(cfg["chAlpha"]):.1f}'
    elif book == 'BIAS':
        defaultProf = '基线现行'
    else:
        defaultProf = '通道L5'
    prof = str(payload.get('exitProfile') or defaultProf)
    sigDate = payload.get('signalDate')
    spot = payload.get('spot')
    cur2 = conn.cursor()
    for item in fills:
        ts = str(item.get('tsCode') or '')
        tenor = tenorCnOf(item.get('tenor') or item.get('tenorKey'))
        k = float(item.get('K') or 0)
        lots = int(item.get('lots') or 0)
        prem = float(item.get('fillPrem') or 0)
        if not ts or lots <= 0 or prem <= 0 or k <= 0:
            raise ValueError('开仓填写不完整')
        cur2.execute(
            """
            INSERT INTO opt_sp_position (
                underlying, book, signal_name, ts_code, tenor, strike, lots,
                entry_date, signal_date, entry_prem, entry_spot, exit_profile, status
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
            """,
            (u, book, sig, ts, tenor, k, lots, execDate, sigDate, prem, spot, prof),
        )
        cur2.execute(
            """
            INSERT INTO opt_sp_trade_log (
                underlying, book, signal_name, action, signal_date, exec_date,
                ts_code, tenor, strike, lots, fill_prem, spot, reason, note
            ) VALUES (%s,%s,%s,'开仓',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (u, book, sig, sigDate, execDate, ts, tenor, k, lots, prem, spot, '开仓', note),
        )


def _confirmClose(
    conn, book: str, fills: list, execDate: str, payload: dict, note: str,
    cfg: dict[str, Any],
) -> None:
    u = cfg['u']
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(
        """
        SELECT * FROM opt_sp_position
        WHERE underlying=%s AND book=%s AND status='open'
        """,
        (u, book),
    )
    opens = cur.fetchall()
    if not opens:
        raise ValueError(f'{book} 无持仓可平')
    byTs = {str(r['ts_code']): r for r in opens}
    reason = str(payload.get('reason') or '手工平仓')
    upd = conn.cursor()
    for item in fills:
        ts = str(item.get('tsCode') or '')
        pos = byTs.get(ts)
        if pos is None:
            raise ValueError(f'找不到持仓 {ts}')
        prem = float(item.get('fillPrem') or 0)
        lots = int(item.get('lots') or pos['lots'])
        if prem <= 0 or lots <= 0:
            raise ValueError('平仓填写不完整')
        pnl = (float(pos['entry_prem']) - prem) * MULT * lots
        hold = (pd.Timestamp(execDate) - pd.Timestamp(pos['entry_date'])).days
        upd.execute(
            """
            UPDATE opt_sp_position
            SET status='closed', close_date=%s, exit_prem=%s, pnl=%s,
                hold_days=%s, close_reason=%s
            WHERE id=%s
            """,
            (execDate, prem, round(pnl, 2), int(hold), reason, int(pos['id'])),
        )
        upd.execute(
            """
            INSERT INTO opt_sp_trade_log (
                underlying, book, signal_name, action, signal_date, exec_date,
                ts_code, tenor, strike, lots, fill_prem, spot, pnl, hold_days, reason, note
            ) VALUES (%s,%s,%s,'平仓',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                u, book, pos['signal_name'], pos['signal_date'], execDate,
                ts, pos['tenor'], float(pos['strike']), lots, prem, payload.get('spot'),
                round(pnl, 2), int(hold), reason, note,
            ),
        )


def _appendCorrectNote(prev: str, oldLots: int, newLots: int, oldPrem: float, newPrem: float) -> str:
    bits: list[str] = []
    if int(newLots) != int(oldLots):
        bits.append(f'{int(oldLots)}→{int(newLots)} 张')
    if abs(float(newPrem) - float(oldPrem)) >= 1e-9:
        bits.append(f'{float(oldPrem):.4f}→{float(newPrem):.4f}')
    piece = '更正 ' + ' / '.join(bits) if bits else ''
    prev = str(prev or '').strip()
    if not piece:
        return prev[:200]
    if not prev:
        return piece[:200]
    if piece in prev:
        return prev[:200]
    return (prev + '；' + piece)[:200]


def _loadOpenLogForCorrect(cur, pos: dict, cfg: dict[str, Any]) -> dict | None:
    u = cfg['u']
    paramsDate = (u, pos['book'], pos['ts_code'], pos['entry_date'])
    cur.execute(
        """
        SELECT id, lots, fill_prem, note FROM opt_sp_trade_log
        WHERE underlying=%s AND book=%s AND ts_code=%s AND action='开仓'
          AND exec_date=%s
        ORDER BY id DESC LIMIT 1
        """,
        paramsDate,
    )
    row = cur.fetchone()
    if row:
        return row
    cur.execute(
        """
        SELECT id, lots, fill_prem, note FROM opt_sp_trade_log
        WHERE underlying=%s AND book=%s AND ts_code=%s AND action='开仓'
        ORDER BY id DESC LIMIT 1
        """,
        (u, pos['book'], pos['ts_code']),
    )
    return cur.fetchone()


def _applyOpenCorrect(cur, pos: dict, lots: int, prem: float, cfg: dict[str, Any]) -> bool:
    oldLots = int(pos['lots'])
    oldPrem = float(pos['entry_prem'])
    if lots == oldLots and abs(prem - oldPrem) < 1e-9:
        return False
    note = _appendCorrectNote('', oldLots, lots, oldPrem, prem)
    cur.execute(
        """
        UPDATE opt_sp_position
        SET lots=%s, entry_prem=%s
        WHERE id=%s AND status='open'
        """,
        (lots, prem, int(pos['id'])),
    )
    if cur.rowcount <= 0:
        raise ValueError(f'持仓 {pos["id"]} 已平或已不存在')
    logRow = _loadOpenLogForCorrect(cur, pos, cfg)
    if logRow:
        logNote = _appendCorrectNote(logRow.get('note') or '', oldLots, lots, oldPrem, prem)
        cur.execute(
            "UPDATE opt_sp_trade_log SET lots=%s, fill_prem=%s, note=%s WHERE id=%s",
            (lots, prem, logNote or note, int(logRow['id'])),
        )
    return True


def correctSpOpen(payload: dict[str, Any]) -> dict[str, Any]:
    """更正未平仓的张数与入场价；同步对应开仓流水。不改 K/合约/账本/日期。"""
    u = str(payload.get('u') or '').strip()
    cfg = spCfg(u)
    if cfg is None:
        return {'ok': False, 'error': f'仅 {spEnabledLabel()} 可更正开仓'}
    fills = payload.get('fills') or []
    if not isinstance(fills, list) or not fills:
        return {'ok': False, 'error': '缺少 fills'}
    conn = pymysql.connect(**DB_CONFIG_TUSHARE)
    try:
        ensureSpPlanTables(conn)
        cur = conn.cursor(pymysql.cursors.DictCursor)
        nChanged = 0
        for item in fills:
            posId = int(item.get('posId') or 0)
            lots = int(item.get('lots') or 0)
            prem = float(item.get('fillPrem') or item.get('entryPrem') or 0)
            if posId <= 0:
                raise ValueError('缺少持仓 id')
            if lots <= 0:
                raise ValueError('张数须为正整数')
            if prem <= 0 or prem >= 10:
                raise ValueError('入场价须在 (0, 10) 内')
            cur.execute(
                """
                SELECT id, book, ts_code, tenor, strike, lots, entry_date, entry_prem, status
                FROM opt_sp_position
                WHERE id=%s AND underlying=%s
                """,
                (posId, cfg['u']),
            )
            pos = cur.fetchone()
            if not pos:
                raise ValueError(f'找不到持仓 {posId}')
            if pos['status'] != 'open':
                raise ValueError(f'持仓 {posId} 已平，不能更正开仓')
            if _applyOpenCorrect(cur, pos, lots, prem, cfg):
                nChanged += 1
        if nChanged <= 0:
            raise ValueError('张数与入场价均无变更')
        conn.commit()
        invalidateSpPlanCache()
        return _querySpPlanUncached(
            u, capitalWan=parseCapitalWan(payload.get('capitalWan')),
        )
    except Exception as e:
        conn.rollback()
        logger.exception('correctSpOpen 失败')
        return {'ok': False, 'error': str(e)}
    finally:
        conn.close()


# 代码更新时间：2026-08-14 21:29
# 代码更新时间：2026-08-14 22:20 下一交易日 trade_cal；期限 CM+中文；确认成交缓存失效
# 代码更新时间：2026-08-14 22:29 规则卡片字段 weightPct/tenorHint/exitLines；期限空格拼接
# 代码更新时间：2026-08-14 22:37 头信息 structure 缩短便于单行
# 代码更新时间：2026-08-14 22:47 planFor 改为下一交易日具体日期
# 代码更新时间：2026-08-14 22:53 开仓 exitHint；合约月+DTE
# 代码更新时间：2026-08-14 22:59 规则卡仓位独立账+期限 chip
# 代码更新时间：2026-08-14 23:10 开仓张数按 capitalWan 缩放
# 代码更新时间：2026-08-14 23:14 规则区 comboBt1y / 单槽 bt1y
# 代码更新时间：2026-08-15 07:48 更正开仓：同步持仓张数/入场价与开仓流水
# 代码更新时间：2026-08-16 10:45 通道新低窗口 L=5→6；Gap/动量/BIAS 仍 L=5
# 代码更新时间：2026-08-16 11:26 Gap 新低窗口 L=5→4；通道仍 L6，动量/BIAS 仍 L5
# 代码更新时间：2026-08-19 10:59 510500 合槽 5/5/3/2 接入；588000 仍 5/5/3/3
# 代码更新时间：2026-08-19 21:22 510500 合槽改为 3/6/5/2（合计 160%）
# 代码更新时间：2026-08-25 07:45 通道/Gap 盘中预演；20日高与Gap P95为交易日窗口
# 代码更新时间：2026-08-25 14:52 预演权利金跳过非当日 QMT last
# 代码更新时间：2026-08-27 13:22 SWR 秒返过期缓存；未接入标的不打库；查询走帧缓存
# 代码更新时间：2026-08-27 14:20 盘中预演 8s SWR，避免 5s 轮询叠 17s 计算
# 代码更新时间：2026-08-28 07:08 预演 TTL 4s，对齐前端轮询
# 代码更新时间：2026-09-02 07:49 159915 合槽 3/5/3/3 接入；通道N25 L10；Gap p97
# 代码更新时间：2026-09-02 08:05 159915 合槽改为 5/5/3/3（合计 160%）
# 代码更新时间：2026-09-23 07:53 peekSpPlan 供壳页 0-RTT 内嵌
