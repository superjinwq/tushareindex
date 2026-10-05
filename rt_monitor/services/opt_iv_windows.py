# -*- coding: utf-8 -*-
"""IV 多窗口分位（Per 1y / Per C / Per A）统一口径。

IV 看板、T 型主表、义务仓优选三处共用本模块：同一个「当前 IV 在历史里的位置」
只保留一套定义。此前 T 型 IVR 走近 252 日窗、IV 看板 Per 走全样本，同一天同一
标的给出相差 30 个分位的两个数，读表时无法判断以哪个为准。

- Per 1y：近 252 交易日 —— 相对最近一年贵不贵
- Per C ：自定义锚点起 —— 波动率中枢跃升后的新常态内贵不贵；锚点按标的配在
          ``config.OPT_IV_PERC_ANCHORS``，未配锚点的标的返回 None
- Per A ：全样本 —— 相对有史以来贵不贵

分位统一为中间秩百分位：``(小于 + 0.5×等于) / n × 100``，值域 0~100。
窗口样本不足 ``MIN_HISTORY`` 个交易日时返回 None（展示 —），不拿十几个点算出的
「分位」参与决策。

性能：窗口预排序一次，逐行只做 bisect。T 型主表单次重算约 240 次分位查询，
朴素逐次全扫实测 85.3ms，预排序 + bisect 为 0.54ms（另加 2.4ms 预排序）。
"""
from __future__ import annotations

import bisect
import json
import os
import threading
from collections import OrderedDict
from datetime import date, datetime
from typing import Callable, Sequence

from logger_config import get_logger
from rt_monitor.core.config import OPT_IV_PERC_ANCHORS

logger = get_logger(__name__)
_percFileLock = threading.Lock()

# 分位窗口最小样本（交易日）。低于此值的窗口不出数，避免小样本分位误导
MIN_HISTORY = 60
# Per 1y 窗口长度（交易日）
WINDOW_1Y = 252
# Per C 样本少于此值时与 Per 1y 窗口高度重叠，title 里提示信息增量有限
PERC_OVERLAP_HINT_N = 300

# (标的, 期限, 方向, asof, 序列口径)
CacheKey = tuple[str, str, str, str, str]


def parseAnchor(value: object) -> date | None:
    """锚点值 → date；空值或非法格式返回 None。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or '').strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def anchorFor(underlyingTs: str) -> date | None:
    """标的的 Per C 锚点；未配置返回 None。"""
    return parseAnchor(OPT_IV_PERC_ANCHORS.get(str(underlyingTs or '').strip()))


def percentileSorted(sortedVals: Sequence[float] | None, x: float | None) -> float | None:
    """中间秩百分位（0~100）。``sortedVals`` 必须已升序；空窗口或非法当前值返回 None。"""
    if not sortedVals or x is None:
        return None
    try:
        cur = float(x)
    except (TypeError, ValueError):
        return None
    # 代表 IV 必为正；NaN 与非正值不参与定位
    if cur != cur or cur <= 0:
        return None
    n = len(sortedVals)
    less = bisect.bisect_left(sortedVals, cur)
    equal = bisect.bisect_right(sortedVals, cur) - less
    return (less + 0.5 * equal) / n * 100.0


class PerWindows:
    """预排序的三窗口容器。

    按 (标的, 期限, 方向, asof) 构建一次，之后逐行只做 bisect 查询。
    ``anchorConfigured`` 区分「配了锚点但样本不足」与「压根没配锚点」——两者
    title 文案不同，前者是数据还不够，后者是该标的本就没有公认的中枢跃升点。
    """

    __slots__ = ('_per1y', '_perc', '_pera', 'anchor', 'anchorConfigured')

    def __init__(
        self,
        *,
        per1y: list[float] | None = None,
        perc: list[float] | None = None,
        pera: list[float] | None = None,
        anchor: date | None = None,
        anchorConfigured: bool = False,
    ) -> None:
        self._per1y = per1y
        self._perc = perc
        self._pera = pera
        self.anchor = anchor
        self.anchorConfigured = anchorConfigured

    @property
    def n1y(self) -> int:
        return len(self._per1y or ())

    @property
    def nc(self) -> int:
        return len(self._perc or ())

    @property
    def na(self) -> int:
        return len(self._pera or ())

    @property
    def isEmpty(self) -> bool:
        return not (self._per1y or self._perc or self._pera)

    def evaluate(self, current: float | None) -> dict[str, float | None]:
        """当前 IV 在三窗口中的分位（0~100）；窗口无效或当前值无效为 None。"""
        return {
            'per1y': percentileSorted(self._per1y, current),
            'perc': percentileSorted(self._perc, current),
            'pera': percentileSorted(self._pera, current),
        }


_EMPTY_WINDOWS = PerWindows()


def emptyWindows() -> PerWindows:
    """无历史可用时的占位（三列均为 —）。"""
    return _EMPTY_WINDOWS


def buildPerWindows(
    dates: Sequence[date] | None,
    vals: Sequence[float] | None,
    *,
    underlyingTs: str = '',
    anchor: date | None = None,
    anchorConfigured: bool | None = None,
) -> PerWindows:
    """由历史序列构建三窗口。

    ``dates`` 与 ``vals`` 等长、一一对应；内部会按日期排序，调用方无需保证顺序。
    仅保留有限正值（代表 IV 必为正）。不足 ``MIN_HISTORY`` 的窗口置 None。
    显式传 ``anchor`` 可绕过配置表（单测与回溯用）。
    """
    if anchorConfigured is None:
        rawAnchor = OPT_IV_PERC_ANCHORS.get(str(underlyingTs or '').strip())
        anchorConfigured = bool(str(rawAnchor or '').strip())
        if anchor is None:
            anchor = parseAnchor(rawAnchor)

    clean: list[tuple[date, float]] = []
    for d, v in zip(dates or (), vals or ()):
        if d is None:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x != x or x <= 0:
            continue
        clean.append((d, x))
    clean.sort(key=lambda t: t[0])

    allVals = [x for _, x in clean]
    per1yRaw = allVals[-WINDOW_1Y:] if len(allVals) > WINDOW_1Y else allVals
    percRaw = [x for d, x in clean if d >= anchor] if anchor is not None else []

    def _prep(vs: list[float]) -> list[float] | None:
        return sorted(vs) if len(vs) >= MIN_HISTORY else None

    return PerWindows(
        per1y=_prep(per1yRaw),
        perc=_prep(percRaw),
        pera=_prep(allVals),
        anchor=anchor,
        anchorConfigured=bool(anchorConfigured),
    )


# ---- 窗口缓存 ----------------------------------------------------------------
# 构建一组窗口要排序上千个点。T 型主表逐行查分位、IV 看板每次盘中覆盖都会撞到
# 同一组 (标的, 期限, 方向, asof) 窗口，不缓存等于把排序成本乘上数百次。
# asof 进键 → 换交易日自然失效；LRU 封顶防跨日累积。
_CACHE_CAP = 512
_cache: OrderedDict[CacheKey, PerWindows] = OrderedDict()
_cacheLock = threading.Lock()

# 序列口径必须进键：IV 看板的序列含 asof 当日（比的是日频序列最新点），T 型为防
# 前视只取 asof 之前（比的是本合约当前 IV）。两者 asof 相同，若不区分口径就会互相
# 串味，且是静默的——数只会差一两个分位，看不出来。
SCOPE_INCL_ASOF = 'incl'
SCOPE_EXCL_ASOF = 'excl'


def cacheKey(
    underlyingTs: str,
    tenor: str,
    callPut: str,
    asof: date | str | None,
    *,
    scope: str = SCOPE_INCL_ASOF,
) -> CacheKey:
    asofS = asof.isoformat() if isinstance(asof, date) else str(asof or '')
    return (str(underlyingTs or ''), str(tenor or ''), str(callPut or ''), asofS, str(scope))


def cachedPerWindows(
    key: CacheKey,
    build: Callable[[], PerWindows],
) -> PerWindows:
    """按键复用三窗口；未命中时调 ``build`` 并落缓存。"""
    with _cacheLock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    built = build()
    with _cacheLock:
        _cache[key] = built
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_CAP:
            _cache.popitem(last=False)
    return built


def peekCached(key: CacheKey) -> PerWindows | None:
    """只读查缓存，不写入。

    盘中覆盖路径只能用 peek：那里没有历史序列可建窗口，若走 ``cachedPerWindows``
    传一个空 builder，空窗口会被写进缓存，之后正常构建路径命中它就让三列永久变 —。
    """
    with _cacheLock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
        return hit


def clearCache() -> None:
    """测试、排障，以及 Per C 锚点保存后失效窗口缓存。"""
    with _cacheLock:
        _cache.clear()


def cacheSize() -> int:
    with _cacheLock:
        return len(_cache)


def perTitle(kind: str, w: PerWindows, *, prefix: str = '') -> str:
    """单列 title：窗口定义 + 锚点 + 样本量；不出数时说明原因。"""
    head = f'{prefix}：' if prefix else ''
    if kind == 'per1y':
        if not w.n1y:
            return f'{head}Per 1y 近 {WINDOW_1Y} 交易日分位 · 样本不足 {MIN_HISTORY} 日'
        return f'{head}Per 1y 近 {WINDOW_1Y} 交易日分位 · n={w.n1y}'
    if kind == 'perc':
        if not w.anchorConfigured:
            return f'{head}Per C 未配置锚点（本标的无公认波动率中枢跃升点）'
        anchorS = w.anchor.isoformat() if w.anchor else '—'
        if not w.nc:
            return f'{head}Per C 自 {anchorS} 起分位 · 样本不足 {MIN_HISTORY} 日'
        text = f'{head}Per C 自 {anchorS} 起分位 · n={w.nc}'
        if w.nc < PERC_OVERLAP_HINT_N:
            text += ' · 与 Per 1y 高度重叠'
        return text
    if kind == 'pera':
        if not w.na:
            return f'{head}Per A 全样本分位 · 样本不足 {MIN_HISTORY} 日'
        return f'{head}Per A 全样本分位 · n={w.na}'
    return ''


def _percAnchorsPath() -> str:
    from rt_monitor.core.config import OPT_IV_PERC_ANCHORS_PATH

    return OPT_IV_PERC_ANCHORS_PATH


def _readFileAnchors() -> dict[str, str]:
    path = _percAnchorsPath()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning('读取 Per C 锚点文件失败: %s', e)
        return {}
    blob: object
    if isinstance(raw, dict) and isinstance(raw.get('anchors'), dict):
        blob = raw['anchors']
    elif isinstance(raw, dict):
        blob = raw
    else:
        return {}
    out: dict[str, str] = {}
    for k, v in blob.items():
        code = str(k).strip()
        if code:
            out[code] = str(v or '').strip()
    return out


def applyPercAnchorLayers() -> None:
    """defaults < env < 页面 JSON。就地更新，使已 import 的 dict 立即生效。"""
    from rt_monitor.core.config import OPT_IV_PERC_ANCHORS_DEFAULT, _parsePercAnchors

    OPT_IV_PERC_ANCHORS.clear()
    OPT_IV_PERC_ANCHORS.update(OPT_IV_PERC_ANCHORS_DEFAULT)
    OPT_IV_PERC_ANCHORS.update(_parsePercAnchors(os.getenv('OPT_IV_PERC_ANCHORS')))
    OPT_IV_PERC_ANCHORS.update(_readFileAnchors())


def canonPercCode(code: str) -> str | None:
    from rt_monitor.core.config import OPT_IV_PERC_ANCHORS_DEFAULT

    s = str(code or '').strip().upper()
    if not s:
        return None
    if s in OPT_IV_PERC_ANCHORS_DEFAULT or s in OPT_IV_PERC_ANCHORS:
        return s
    if len(s) == 6 and s.isdigit():
        for k in list(OPT_IV_PERC_ANCHORS_DEFAULT) + list(OPT_IV_PERC_ANCHORS):
            if k.startswith(s + '.'):
                return k
    return None


def percAnchorCatalog() -> list[dict[str, str]]:
    from rt_monitor.services.opt_iv_board import OPT_IV_BOARD_UNDERLYINGS

    items: list[dict[str, str]] = []
    for tsCode, nameCn in OPT_IV_BOARD_UNDERLYINGS:
        items.append({
            'ts_code': tsCode,
            'name': str(nameCn),
            'date': str(OPT_IV_PERC_ANCHORS.get(tsCode) or ''),
        })
    return items


def _invalidatePercDependentCaches() -> None:
    clearCache()
    try:
        from rt_monitor.services.opt_t_board import clearOptTBoardCache

        clearOptTBoardCache()
    except Exception:
        logger.exception('清空 T 板缓存失败')
    try:
        from rt_monitor.services.opt_iv_board import clearOptIvBoardCache

        clearOptIvBoardCache()
    except Exception:
        logger.exception('清空 IV 看板缓存失败')


def savePercAnchors(updates: dict[str, str]) -> tuple[list[dict[str, str]] | None, str | None]:
    """写入页面覆盖层并刷新内存配置。返回 (catalog, error)。"""
    from rt_monitor.core.config import OPT_IV_PERC_ANCHORS_DEFAULT

    if not isinstance(updates, dict):
        return None, 'anchors 须为对象'
    normalized: dict[str, str] = {}
    for rawCode, rawDate in updates.items():
        code = canonPercCode(str(rawCode))
        if code is None:
            return None, f'未知标的 {rawCode}'
        day = str(rawDate or '').strip()
        if day:
            parsed = parseAnchor(day)
            if parsed is None:
                return None, f'{code} 锚点须为 YYYY-MM-DD 或留空'
            day = parsed.isoformat()
        normalized[code] = day
    with _percFileLock:
        merged = _readFileAnchors()
        merged.update(normalized)
        known = set(OPT_IV_PERC_ANCHORS_DEFAULT) | set(OPT_IV_PERC_ANCHORS)
        payload = {'anchors': {k: merged[k] for k in merged if k in known}}
        path = _percAnchorsPath()
        tmp = path + '.tmp'
        parent = os.path.dirname(path) or '.'
        os.makedirs(parent, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, path)
        applyPercAnchorLayers()
    _invalidatePercDependentCaches()
    return percAnchorCatalog(), None


applyPercAnchorLayers()

# 代码更新时间：2026-09-21 06:31

